"""PostgreSQL adapters for the M1 demo workflow."""

import asyncio
import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from evorec.domain.errors import (
    AccessDenied,
    FeedbackSourceMismatch,
    HistoryConflict,
    IdempotencyConflict,
    ResourceNotFound,
    SessionEpochConflict,
    SnapshotMismatch,
)
from evorec.domain.models import (
    CatalogSnapshot,
    CreatedSession,
    FeedbackCommand,
    FeedbackKind,
    FeedbackResult,
    RankedBatch,
    ReadinessReport,
    RecommendationCommand,
    RecommendationResult,
    RequestBinding,
    RequestContext,
    ScoredCandidate,
    SessionSnapshot,
    Strategy,
    detail_exposure_event_id,
    detail_exposure_payload_sha256,
)


class PostgresDemoBackend:
    """Persist sessions, admitted requests, and completed recommendation items."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @staticmethod
    def _token_sha256(access_token: str) -> str:
        return hashlib.sha256(access_token.encode("utf-8")).hexdigest()

    @staticmethod
    def _snapshot(row: dict[str, object]) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=row["session_id"],
            epoch=row["epoch"],
            history_version=row["history_version"],
            history=tuple(row["history"]),
            hidden_items=frozenset(row["hidden_items"]),
        )

    @staticmethod
    def _authorize(row: dict[str, object], access_token: str) -> None:
        if not hmac.compare_digest(
            str(row["owner_token_sha256"]),
            PostgresDemoBackend._token_sha256(access_token),
        ):
            raise AccessDenied("session token is invalid")

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, row_factory=dict_row)

    async def create_session(self) -> CreatedSession:
        return await asyncio.to_thread(self._create_session)

    def _create_session(self) -> CreatedSession:
        snapshot = SessionSnapshot(uuid4(), 0, 0, (), frozenset())
        access_token = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions (session_id, owner_token_sha256) VALUES (%s, %s)",
                (snapshot.session_id, self._token_sha256(access_token)),
            )
        return CreatedSession(snapshot, access_token)

    async def get_session(self, session_id: UUID, access_token: str) -> SessionSnapshot:
        return await asyncio.to_thread(self._get_session, session_id, access_token)

    def _get_session(self, session_id: UUID, access_token: str) -> SessionSnapshot:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT session_id, owner_token_sha256, epoch, history_version,
                       history, hidden_items
                FROM sessions WHERE session_id = %s
                """,
                (session_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise ResourceNotFound("session does not exist")
        self._authorize(row, access_token)
        return self._snapshot(row)

    async def reset_session(self, session_id: UUID, access_token: str) -> SessionSnapshot:
        return await asyncio.to_thread(self._reset_session, session_id, access_token)

    def _reset_session(self, session_id: UUID, access_token: str) -> SessionSnapshot:
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT owner_token_sha256 FROM sessions WHERE session_id = %s FOR UPDATE",
                (session_id,),
            )
            owner = cursor.fetchone()
            if owner is None:
                raise ResourceNotFound("session does not exist")
            self._authorize(owner, access_token)
            cursor = connection.execute(
                """
                UPDATE sessions
                SET epoch = epoch + 1, history_version = history_version + 1,
                    history = '[]'::jsonb, hidden_items = '[]'::jsonb,
                    favorite_items = '[]'::jsonb, updated_at = now()
                WHERE session_id = %s
                RETURNING session_id, owner_token_sha256, epoch, history_version,
                          history, hidden_items
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            connection.execute(
                "DELETE FROM session_item_states WHERE session_id = %s",
                (session_id,),
            )
        assert row is not None
        return self._snapshot(row)

    @asynccontextmanager
    async def acquire(self, command: RecommendationCommand):
        context = await asyncio.to_thread(self._admit, command)
        try:
            yield context
        except BaseException:
            await asyncio.to_thread(self._mark_failed, command.request_id)
            raise

    def _admit(self, command: RecommendationCommand) -> RequestContext:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT session_id, owner_token_sha256, epoch, history_version,
                       history, hidden_items
                FROM sessions WHERE session_id = %s FOR UPDATE
                """,
                (command.session_id,),
            )
            session_row = cursor.fetchone()
            if session_row is None:
                raise ResourceNotFound("session does not exist")
            self._authorize(session_row, command.session_token)
            session = self._snapshot(session_row)
            if session.history_version != command.expected_history_version:
                raise HistoryConflict("history changed before admission")

            cursor = connection.execute(
                """
                SELECT active_bundle_id, exclusion_version
                FROM catalog_control WHERE singleton = 1 AND admission_open
                FOR SHARE
                """
            )
            control = cursor.fetchone()
            if control is None or control["active_bundle_id"] is None:
                raise RuntimeError("catalog admission is not ready")
            cursor = connection.execute(
                """
                SELECT bi.item_id
                FROM bundle_items bi JOIN items i ON i.item_id = bi.item_id
                WHERE bi.bundle_id = %s AND i.is_active
                """,
                (control["active_bundle_id"],),
            )
            eligible = frozenset(row["item_id"] for row in cursor.fetchall())
            catalog = CatalogSnapshot(
                control["active_bundle_id"], control["exclusion_version"], eligible,
            )
            context = RequestContext(command.request_id, session, catalog)
            connection.execute(
                """
                INSERT INTO recommendation_requests (
                    request_id, session_id, session_epoch, history_version,
                    history_snapshot, hidden_snapshot, bundle_id, exclusion_version,
                    requested_strategy, requested_k, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'accepted')
                """,
                (
                    command.request_id, command.session_id, session.epoch,
                    session.history_version, Jsonb(list(session.history)),
                    Jsonb(sorted(session.hidden_items)),
                    catalog.bundle_id, catalog.exclusion_version, command.strategy, command.k,
                ),
            )
        return context

    def _mark_failed(self, request_id: UUID) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE recommendation_requests
                SET status = 'failed', failure_code = 'execution_failed', updated_at = now()
                WHERE request_id = %s AND status = 'accepted'
                """,
                (request_id,),
            )

    async def record_feedback(self, command: FeedbackCommand) -> FeedbackResult:
        return await asyncio.to_thread(self._record_feedback, command)

    @staticmethod
    def _ensure_detail_exposure(
        connection: psycopg.Connection,
        command: FeedbackCommand,
        session_epoch: int,
        outcome_version: int,
    ) -> UUID | None:
        if command.kind != FeedbackKind.DETAIL_VIEW:
            return None
        exposure_event_id = detail_exposure_event_id(command.event_id)
        payload_sha256 = detail_exposure_payload_sha256(command.event_id)
        connection.execute(
            """
            INSERT INTO feedback_events (
                event_id, session_id, request_id, item_id, session_epoch,
                event_kind, desired_state, observed_at, payload_sha256,
                evidence, outcome_history_version
            ) VALUES (%s, %s, %s, %s, %s, 'exposure', NULL, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                exposure_event_id,
                command.session_id,
                command.request_id,
                command.item_id,
                session_epoch,
                command.observed_at,
                payload_sha256,
                Jsonb(
                    {
                        "source": "detail_view_backfill",
                        "detail_event_id": str(command.event_id),
                    }
                ),
                outcome_version,
            ),
        )
        cursor = connection.execute(
            """
            SELECT session_id, request_id, item_id, session_epoch, event_kind,
                   payload_sha256, outcome_history_version
            FROM feedback_events WHERE event_id = %s
            """,
            (exposure_event_id,),
        )
        stored = cursor.fetchone()
        expected = (
            command.session_id,
            command.request_id,
            command.item_id,
            session_epoch,
            "exposure",
            payload_sha256,
            outcome_version,
        )
        actual = (
            stored["session_id"],
            stored["request_id"],
            stored["item_id"],
            stored["session_epoch"],
            stored["event_kind"],
            str(stored["payload_sha256"]),
            stored["outcome_history_version"],
        )
        if actual != expected:
            raise IdempotencyConflict(
                "derived exposure event ID was already used by another event"
            )
        return exposure_event_id

    def _record_feedback(self, command: FeedbackCommand) -> FeedbackResult:
        with self._connect() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (str(command.event_id),),
            )
            cursor = connection.execute(
                """
                SELECT session_id, owner_token_sha256, epoch, history_version,
                       history, hidden_items, favorite_items
                FROM sessions WHERE session_id = %s FOR UPDATE
                """,
                (command.session_id,),
            )
            session = cursor.fetchone()
            if session is None:
                raise ResourceNotFound("session does not exist")
            self._authorize(session, command.session_token)

            cursor = connection.execute(
                """
                SELECT payload_sha256, session_epoch, outcome_history_version
                FROM feedback_events WHERE event_id = %s
                """,
                (command.event_id,),
            )
            previous = cursor.fetchone()
            if previous is not None:
                if not hmac.compare_digest(
                    str(previous["payload_sha256"]), command.payload_sha256
                ):
                    raise IdempotencyConflict(
                        "event ID was already used for different feedback"
                    )
                exposure_event_id = self._ensure_detail_exposure(
                    connection,
                    command,
                    previous["session_epoch"],
                    previous["outcome_history_version"],
                )
                return FeedbackResult(
                    command.event_id,
                    command.session_id,
                    previous["session_epoch"],
                    previous["outcome_history_version"],
                    True,
                    exposure_event_id,
                )

            cursor = connection.execute(
                """
                SELECT r.session_id, r.session_epoch
                FROM recommendation_requests r
                JOIN request_items ri ON ri.request_id = r.request_id
                WHERE r.request_id = %s AND ri.item_id = %s
                """,
                (command.request_id, command.item_id),
            )
            source = cursor.fetchone()
            if source is None or source["session_id"] != command.session_id:
                raise FeedbackSourceMismatch(
                    "feedback does not refer to an item returned to this session"
                )
            if source["session_epoch"] != session["epoch"]:
                raise SessionEpochConflict(
                    "feedback request belongs to an earlier session epoch"
                )

            history = list(session["history"])
            hidden = set(session["hidden_items"])
            favorites = set(session["favorite_items"])
            changed = False
            if command.kind == FeedbackKind.DETAIL_VIEW:
                history.append(command.item_id)
                changed = True
            elif command.kind == FeedbackKind.HIDE_SET:
                before = command.item_id in hidden
                if command.desired_state:
                    hidden.add(command.item_id)
                else:
                    hidden.discard(command.item_id)
                changed = before != command.desired_state
            elif command.kind == FeedbackKind.FAVORITE_SET:
                before = command.item_id in favorites
                if command.desired_state:
                    favorites.add(command.item_id)
                else:
                    favorites.discard(command.item_id)
                changed = before != command.desired_state

            outcome_version = session["history_version"] + int(changed)
            if changed:
                connection.execute(
                    """
                    UPDATE sessions
                    SET history_version = %s, history = %s, hidden_items = %s,
                        favorite_items = %s, updated_at = now()
                    WHERE session_id = %s
                    """,
                    (
                        outcome_version,
                        Jsonb(history),
                        Jsonb(sorted(hidden)),
                        Jsonb(sorted(favorites)),
                        command.session_id,
                    ),
                )

            evidence = None
            if command.kind == FeedbackKind.EXPOSURE:
                evidence = Jsonb(
                    {
                        "visible_ratio": command.visible_ratio,
                        "visible_duration_ms": command.visible_duration_ms,
                    }
                )
            connection.execute(
                """
                INSERT INTO feedback_events (
                    event_id, session_id, request_id, item_id, session_epoch,
                    event_kind, desired_state, observed_at, payload_sha256,
                    evidence, outcome_history_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    command.event_id,
                    command.session_id,
                    command.request_id,
                    command.item_id,
                    session["epoch"],
                    command.kind.value,
                    command.desired_state,
                    command.observed_at,
                    command.payload_sha256,
                    evidence,
                    outcome_version,
                ),
            )
            exposure_event_id = self._ensure_detail_exposure(
                connection,
                command,
                session["epoch"],
                outcome_version,
            )
            if command.kind in {FeedbackKind.FAVORITE_SET, FeedbackKind.HIDE_SET}:
                connection.execute(
                    """
                    INSERT INTO session_item_states (
                        session_id, item_id, is_favorite, is_hidden, updated_by_event_id
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (session_id, item_id) DO UPDATE
                    SET is_favorite = EXCLUDED.is_favorite,
                        is_hidden = EXCLUDED.is_hidden,
                        updated_by_event_id = EXCLUDED.updated_by_event_id,
                        updated_at = now()
                    """,
                    (
                        command.session_id,
                        command.item_id,
                        command.item_id in favorites,
                        command.item_id in hidden,
                        command.event_id,
                    ),
                )
        return FeedbackResult(
            command.event_id,
            command.session_id,
            session["epoch"],
            outcome_version,
            False,
            exposure_event_id,
        )

    async def rank(self, context: RequestContext, command: RecommendationCommand) -> RankedBatch:
        actual = command.strategy
        fallback = None
        if command.strategy == Strategy.ADAPTIVE:
            actual = Strategy.POPULAR
        elif command.strategy != Strategy.POPULAR:
            actual = Strategy.POPULAR
            fallback = "strategy_not_loaded_in_postgres_demo"
        candidates = tuple(
            ScoredCandidate(item_id, 1.0 / position, "postgres-demo-popular")
            for position, item_id in enumerate(sorted(context.catalog.eligible_items), start=1)
        )
        return RankedBatch(context.binding, actual, candidates, fallback)

    async def save(self, result: RecommendationResult) -> None:
        await asyncio.to_thread(self._save, result)

    def _save(self, result: RecommendationResult) -> None:
        binding = result.binding
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT session_id, session_epoch, history_version, bundle_id,
                       exclusion_version, requested_strategy, actual_strategy,
                       fallback_reason, status
                FROM recommendation_requests WHERE request_id = %s FOR UPDATE
                """,
                (binding.request_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise SnapshotMismatch("result does not match an accepted request")
            stored = RequestBinding(
                binding.request_id, row["session_id"], row["session_epoch"],
                row["history_version"], row["bundle_id"], row["exclusion_version"],
            )
            if stored != binding:
                raise SnapshotMismatch("result binding differs from the accepted request")
            if row["status"] == "completed":
                cursor = connection.execute(
                    """
                    SELECT item_id, score, source
                    FROM request_items WHERE request_id = %s ORDER BY position
                    """,
                    (binding.request_id,),
                )
                stored_items = tuple(
                    (item["item_id"], item["score"], item["source"])
                    for item in cursor.fetchall()
                )
                submitted_items = tuple(
                    (item.item_id, item.score, item.source) for item in result.items
                )
                if (
                    row["requested_strategy"] != result.requested_strategy
                    or row["actual_strategy"] != result.actual_strategy
                    or row["fallback_reason"] != result.fallback_reason
                    or stored_items != submitted_items
                ):
                    raise SnapshotMismatch("completed request has different result content")
                return
            if row["status"] != "accepted":
                raise SnapshotMismatch("request is not in an accepted state")
            if result.items:
                cursor = connection.cursor()
                cursor.executemany(
                    """
                    INSERT INTO request_items (request_id, item_id, position, score, source)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    [
                        (binding.request_id, item.item_id, position, item.score, item.source)
                        for position, item in enumerate(result.items, start=1)
                    ],
                )
            connection.execute(
                """
                UPDATE recommendation_requests
                SET actual_strategy = %s, status = 'completed', fallback_reason = %s,
                    completed_at = now(), updated_at = now()
                WHERE request_id = %s
                """,
                (result.actual_strategy, result.fallback_reason, binding.request_id),
            )


class PostgresDemoReadiness:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    async def check(self) -> ReadinessReport:
        return await asyncio.to_thread(self._check)

    def _check(self) -> ReadinessReport:
        blockers: list[str] = ["model_runtime_not_loaded"]
        try:
            with psycopg.connect(self.database_url) as connection:
                cursor = connection.execute(
                    """
                    SELECT active_bundle_id, admission_open
                    FROM catalog_control WHERE singleton = 1
                    """
                )
                row = cursor.fetchone()
        except psycopg.Error:
            return ReadinessReport(("database_not_connected", "model_runtime_not_loaded"))
        if row is None or row[0] is None:
            blockers.append("catalog_bundle_not_loaded")
        if row is None or not row[1]:
            blockers.append("publication_barrier_closed")
        return ReadinessReport(tuple(blockers))
