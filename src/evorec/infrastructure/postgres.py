"""PostgreSQL adapters for the M1 demo workflow."""

import asyncio
import hashlib
import hmac
import os
import secrets
from dataclasses import replace
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from threading import Lock
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row, namedtuple_row
from psycopg.types.json import Jsonb

from evorec.domain.errors import (
    AccessDenied,
    FeedbackSourceMismatch,
    HistoryConflict,
    IdempotencyConflict,
    IdempotencyInProgress,
    IdempotencyReplay,
    ResourceNotFound,
    SessionEpochConflict,
    SnapshotMismatch,
    ManagementError,
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
from evorec.domain.session_profiles import initial_history
from evorec.infrastructure.r06_admission import (
    ManagedR06Runtime, capture_model, decode_model, encode_model,
    validate_captured_context,
)
from evorec.infrastructure.r06_async import R06CPUQueue, R06RankingPort, _drain
from evorec.infrastructure.recommendation_execution import RecommendationExecution

if TYPE_CHECKING:
    from evorec.infrastructure.management import CatalogManager
    from evorec.infrastructure.model_runtime import RuntimeBundle


class PostgresDemoBackend:
    """Persist sessions, admitted requests, and completed recommendation items."""

    def __init__(self, database_url: str, *, r06_enabled: bool | None = None,
                 r06_content_backend: str | None = None, r06_ranker_backend: str | None = None) -> None:
        self.database_url = database_url
        self.runtime: RuntimeBundle | None = None
        self.runtimes: dict[str, RuntimeBundle] = {}
        self.manager: CatalogManager | None = None
        self._execution_lock = Lock()
        self._executions: dict[UUID, RecommendationExecution] = {}
        if r06_enabled is not None and type(r06_enabled) is not bool:
            raise ValueError("R06 serving flag must be a boolean")
        enabled = (os.getenv("EVOREC_R06_SERVING_ENABLED", "0") if r06_enabled is None
                   else "1" if r06_enabled else "0")
        backend = (os.getenv("EVOREC_R06_CONTENT_BACKEND", "stdlib")
                   if r06_content_backend is None else r06_content_backend)
        ranker_backend = (os.getenv("EVOREC_R06_RANKER_BACKEND", "stdlib")
                          if r06_ranker_backend is None else r06_ranker_backend)
        if enabled not in {"0", "1"} or backend not in {"stdlib", "numpy"} or ranker_backend not in {"stdlib", "numpy"}:
            raise ValueError("R06 serving requires an explicit 0/1 flag and stdlib/numpy backend")
        self.r06_enabled = enabled == "1"
        self.r06_content_backend = backend
        self.r06_ranker_backend = ranker_backend
        self.r06_queue = R06CPUQueue() if self.r06_enabled else None

    async def aclose(self):
        if self.r06_queue is not None:
            await self.r06_queue.aclose()

    def _capture_context(self, connection, request_id, session, control):
        bundle_id = control["active_bundle_id"]
        runtime = self.runtimes.get(str(bundle_id))
        kind = connection.execute("SELECT runtime_kind FROM bundle_versions WHERE bundle_id = %s",
                                  (bundle_id,)).fetchone()
        model = None
        if kind and kind["runtime_kind"] == "r06-frozen-bundle-v1":
            if not self.r06_enabled or not isinstance(runtime, ManagedR06Runtime):
                raise ManagementError("r06_runtime_unavailable", "approved R06 runtime is unavailable", 503)
            # One actual-row read supplies eligibility and content, not two READ COMMITTED views.
            # Compact rows are local to this large read; other queries retain dict_row.
            # Compute the digest from actual text in this same SQL view. No
            # mutable stored hash is trusted; raw time and all members remain.
            with connection.cursor(row_factory=namedtuple_row, binary=True) as cursor:
                rows = cursor.execute(
                    "SELECT bi.item_id, bi.internal_item_id, i.is_active, "
                    "pg_catalog.sha256(pg_catalog.convert_to(i.r06_model_text, 'UTF8')) AS r06_text_sha256, "
                    "i.r06_first_seen_ms "
                    "FROM bundle_items bi JOIN items i ON i.item_id=bi.item_id "
                    "WHERE bi.bundle_id=%s ORDER BY bi.internal_item_id", (bundle_id,),
                ).fetchall()
            eligible = frozenset(row.item_id for row in rows if row.is_active)
            catalog = CatalogSnapshot(bundle_id, control["exclusion_version"], eligible)
            model = capture_model(connection, runtime, session, catalog, rows)
        elif isinstance(runtime, ManagedR06Runtime):
            raise ManagementError("r06_snapshot_changed", "registered runtime kind differs", 503)
        else:
            eligible = frozenset(row["item_id"] for row in connection.execute(
                "SELECT bi.item_id FROM bundle_items bi JOIN items i ON i.item_id = bi.item_id "
                "WHERE bi.bundle_id = %s AND i.is_active", (bundle_id,),
            ).fetchall())
            catalog = CatalogSnapshot(bundle_id, control["exclusion_version"], eligible)
        context = RequestContext(request_id, session, catalog, model)
        if model is not None:
            # Validate full history/state limits before any accepted request is written.
            # Actual rows just produced the seal; avoid hashing it again here.
            # Ranking/restoration still verify the persisted identity in full.
            validate_captured_context(runtime.bundle, context)
        return context

    def activate_runtime(self, runtime: "RuntimeBundle") -> None:
        self.runtimes[runtime.bundle_id] = runtime
        self.runtime = runtime

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
            favorite_items=frozenset(row["favorite_items"]),
            profile_id=row["seed_user_id"] or "new",
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

    async def create_session(self, profile_id: str = "new") -> CreatedSession:
        if profile_id == "sample" and self.manager is not None:
            await asyncio.to_thread(self.manager.ensure_ready)
        return await asyncio.to_thread(self._create_session, profile_id)

    def _profile_history(self, connection, profile_id):
        history = initial_history(profile_id)
        if not history:
            return history
        active = connection.execute(
            "SELECT b.bundle_id, b.runtime_kind FROM catalog_control c "
            "JOIN bundle_versions b ON b.bundle_id=c.active_bundle_id "
            "WHERE c.singleton=1 AND c.admission_open FOR SHARE OF c",
        ).fetchone()
        if active is None:
            raise ValueError("sample profile requires an active catalog")
        runtime = self.runtimes.get(str(active["bundle_id"]))
        if active["runtime_kind"] == "r06-frozen-bundle-v1":
            if not self.r06_enabled or not isinstance(runtime, ManagedR06Runtime):
                raise ValueError("sample profile requires the approved R06 runtime")
            features = runtime.bundle.adapter.features
            # Fixed package order, not target labels, user data or quality tuning.
            index = next((i for i, metadata in enumerate(features._metadata)
                          if metadata.training_item and features._present[i]), None)
            if index is None:
                raise ValueError("approved R06 catalog has no represented training sample")
            history = (features.item_ids[index],)
            approved = runtime.catalog_items[history[0]]
        elif isinstance(runtime, ManagedR06Runtime):
            raise ValueError("registered sample runtime kind differs")
        row = connection.execute(
            "SELECT bi.internal_item_id, i.r06_model_text, i.r06_first_seen_ms, "
            "floor(extract(epoch FROM clock_timestamp())*1000)::bigint AS now_ms "
            "FROM bundle_items bi JOIN items i ON i.item_id=bi.item_id "
            "WHERE bi.bundle_id=%s AND bi.item_id=%s AND i.is_active",
            (active["bundle_id"], history[0]),
        ).fetchone()
        if row is None:
            raise ValueError("fixed sample item is unavailable in the active catalog")
        if isinstance(runtime, ManagedR06Runtime) and (
                row["internal_item_id"] != index or row["r06_model_text"] != approved.text
                or row["r06_first_seen_ms"] != approved.first_seen_ms
                or approved.first_seen_ms >= row["now_ms"]):
            raise ValueError("fixed R06 sample representation or time differs")
        return history

    def _create_session(self, profile_id: str = "new") -> CreatedSession:
        access_token = secrets.token_urlsafe(32)
        with self._connect() as connection:
            history = self._profile_history(connection, profile_id)
            snapshot = SessionSnapshot(uuid4(), 0, 0, history, frozenset(), profile_id=profile_id)
            connection.execute(
                "INSERT INTO sessions (session_id, owner_token_sha256, seed_user_id, history) "
                "VALUES (%s, %s, %s, %s)",
                (snapshot.session_id, self._token_sha256(access_token), profile_id, Jsonb(list(history))),
            )
        return CreatedSession(snapshot, access_token)

    async def get_session(self, session_id: UUID, access_token: str) -> SessionSnapshot:
        return await asyncio.to_thread(self._get_session, session_id, access_token)

    async def snapshot_for_comparison(self, command: RecommendationCommand) -> RequestContext:
        if self.manager is not None:
            await asyncio.to_thread(self.manager.ensure_ready)
        return await asyncio.to_thread(self._comparison_snapshot, command)

    def _comparison_snapshot(self, command: RecommendationCommand) -> RequestContext:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            row = connection.execute(
                "SELECT session_id, owner_token_sha256, epoch, history_version, history, "
                "hidden_items, favorite_items, seed_user_id FROM sessions WHERE session_id = %s",
                (command.session_id,),
            ).fetchone()
            if row is None:
                raise ResourceNotFound("session does not exist")
            self._authorize(row, command.session_token)
            session = self._snapshot(row)
            if session.history_version != command.expected_history_version:
                raise HistoryConflict("history changed before comparison")
            control = connection.execute(
                "SELECT active_bundle_id, exclusion_version FROM catalog_control "
                "WHERE singleton = 1 AND admission_open",
            ).fetchone()
            if control is None or control["active_bundle_id"] is None:
                raise RuntimeError("catalog admission is not ready")
            context = self._capture_context(connection, command.request_id, session, control)
        return context

    def _get_session(self, session_id: UUID, access_token: str) -> SessionSnapshot:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT session_id, owner_token_sha256, epoch, history_version,
                       history, hidden_items, favorite_items, seed_user_id
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
                "SELECT owner_token_sha256, seed_user_id FROM sessions WHERE session_id = %s FOR UPDATE",
                (session_id,),
            )
            owner = cursor.fetchone()
            if owner is None:
                raise ResourceNotFound("session does not exist")
            self._authorize(owner, access_token)
            history = self._profile_history(connection, owner["seed_user_id"] or "new")
            cursor = connection.execute(
                """
                UPDATE sessions
                SET epoch = epoch + 1, history_version = history_version + 1,
                    history = %s, hidden_items = '[]'::jsonb,
                    favorite_items = '[]'::jsonb, updated_at = now()
                WHERE session_id = %s
                RETURNING session_id, owner_token_sha256, epoch, history_version,
                          history, hidden_items, favorite_items, seed_user_id
                """,
                (Jsonb(list(history)), session_id),
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
        if self.manager is not None:
            readiness = asyncio.create_task(asyncio.to_thread(self.manager.ensure_ready))
            try:
                await asyncio.shield(readiness)
            except asyncio.CancelledError:
                # Recovery owns a connection/lock before an execution lease exists.
                await _drain(readiness)
                raise
        execution = RecommendationExecution(self, command)
        admission = asyncio.create_task(asyncio.to_thread(execution.admit))
        try:
            try:
                context = await asyncio.shield(admission)
                yield context
            except BaseException:
                # Retain the execution lock until all owned writes have drained.
                await _drain(admission)
                if execution.admitted:
                    failure = asyncio.create_task(asyncio.to_thread(self._mark_failed, command.request_id))
                    await _drain(failure)
                raise
        finally:
            closing = asyncio.create_task(asyncio.to_thread(execution.close))
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                await _drain(closing)
                raise

    @staticmethod
    def _matching_request(previous, command):
        if (previous["session_id"] != command.session_id
                or previous["history_version"] != command.expected_history_version
                or previous["requested_strategy"] != command.strategy
                or previous["requested_k"] != command.k):
            raise IdempotencyConflict("recommendation key was used for different input")

    def _execution(self, request_id):
        with self._execution_lock:
            return self._executions.get(request_id)

    def _admit(self, command: RecommendationCommand) -> RequestContext:
        with self._connect() as connection:
            # Admission only reads session state. Shared readers may capture
            # distinct keys concurrently, but feedback/reset retain FOR UPDATE
            # and cannot change this snapshot until admission commits. The
            # request-scoped execution lease still serializes identical keys.
            cursor = connection.execute(
                """
                SELECT session_id, owner_token_sha256, epoch, history_version,
                       history, hidden_items, favorite_items, seed_user_id
                FROM sessions WHERE session_id = %s FOR SHARE
                """,
                (command.session_id,),
            )
            session_row = cursor.fetchone()
            if session_row is None:
                raise ResourceNotFound("session does not exist")
            self._authorize(session_row, command.session_token)
            session = self._snapshot(session_row)
            previous = connection.execute(
                """
                SELECT session_id, session_epoch, history_version, bundle_id,
                       exclusion_version, requested_strategy, requested_k,
                       actual_strategy, fallback_reason, status, model_snapshot, execution_owner, failure_code
                FROM recommendation_requests WHERE request_id = %s FOR UPDATE
                """,
                (command.request_id,),
            ).fetchone()
            if previous is not None:
                self._matching_request(previous, command)
                if previous["status"] == "accepted":
                    execution = self._execution(command.request_id)
                    if (previous["execution_owner"] is not None and execution is not None
                            and previous["execution_owner"] != execution.owner):
                        execution.assert_held()
                        connection.execute(
                            "UPDATE recommendation_requests SET status = 'failed', "
                            "failure_code = 'execution_interrupted', updated_at = clock_timestamp() "
                            "WHERE request_id = %s", (command.request_id,),
                        )
                        # Preserve the terminal outcome despite returning an error.
                        connection.commit()
                        raise ManagementError("recommendation_interrupted",
                                              "old execution lease was lost; explicitly submit a new key", 409)
                    raise IdempotencyInProgress("recommendation is still in progress")
                if previous["status"] != "completed":
                    if previous["failure_code"] == "execution_interrupted":
                        raise ManagementError("recommendation_interrupted",
                                              "old execution was interrupted; explicitly submit a new key", 409)
                    raise IdempotencyConflict("recommendation key belongs to a failed request")
                items = connection.execute(
                    """
                    SELECT item_id, score, source FROM request_items
                    WHERE request_id = %s ORDER BY position
                    """,
                    (command.request_id,),
                ).fetchall()
                binding = RequestBinding(
                    command.request_id, previous["session_id"], previous["session_epoch"],
                    previous["history_version"], previous["bundle_id"],
                    previous["exclusion_version"],
                )
                model = decode_model(previous["model_snapshot"])
                raise IdempotencyReplay(RecommendationResult(
                    binding, Strategy(previous["requested_strategy"]),
                    Strategy(previous["actual_strategy"]),
                    tuple(ScoredCandidate(row["item_id"], row["score"], row["source"])
                          for row in items),
                    previous["fallback_reason"],
                    model.model_version if model else None, model.timestamp_ms if model else None,
                ))
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
            context = self._capture_context(connection, command.request_id, session, control)
            catalog = context.catalog
            execution = self._execution(command.request_id)
            if execution is None:
                raise ManagementError("recommendation_execution_lost", "fresh admission requires an execution lease", 503)
            execution.assert_held()
            connection.execute(
                """
                INSERT INTO recommendation_requests (
                    request_id, session_id, session_epoch, history_version,
                    history_snapshot, hidden_snapshot, bundle_id, exclusion_version,
                    requested_strategy, requested_k, model_snapshot, execution_owner, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'accepted')
                """,
                (
                    command.request_id, command.session_id, session.epoch,
                    session.history_version, Jsonb(list(session.history)),
                    Jsonb(sorted(session.hidden_items)),
                    catalog.bundle_id, catalog.exclusion_version, command.strategy, command.k,
                    Jsonb(encode_model(context.model)) if context.model else None,
                    execution.owner,
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
                if changed and command.desired_state:
                    history.append(command.item_id)

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
        runtime = self.runtimes.get(str(context.catalog.bundle_id))
        if context.model is not None or isinstance(runtime, ManagedR06Runtime):
            if (not self.r06_enabled or self.r06_queue is None or not isinstance(runtime, ManagedR06Runtime)):
                raise ManagementError("r06_runtime_unavailable", "frozen R06 runtime is unavailable", 503)
            port = await asyncio.to_thread(R06RankingPort.from_snapshot, self.r06_queue, runtime.bundle, context)
            if command.strategy in {Strategy.DENSE, Strategy.ADAPTIVE}:
                return await port.rank(context, replace(command, strategy=Strategy.DENSE))
            # Explicit baseline: same frozen eligibility/time/full-seen exclusions.
            def baseline():
                bundle = runtime.bundle
                popular = bundle.adapter.retrieval.popular(
                    port.request.full_seen, port.request.timestamp_ms,
                    eligible_items=context.catalog.eligible_items, k=command.k)
                return RankedBatch(context.binding, Strategy.POPULAR, tuple(
                    ScoredCandidate(item, heat, "r06-training-recent-popular-v1")
                    for item, heat in popular),
                    None if command.strategy == Strategy.POPULAR else "strategy_not_loaded_in_r06",
                    runtime.bundle.model_version)
            return await self.r06_queue.run(baseline)
        if (runtime is not None and runtime.bundle_id == str(context.catalog.bundle_id)
                and command.strategy == Strategy.DENSE):
            candidate_ids = tuple(sorted(context.catalog.eligible_items))[:runtime.ranking_budget]
            limited = len(context.catalog.eligible_items) > runtime.ranking_budget
            if candidate_ids:
                history = tuple(item for item in context.session.history
                                if item in runtime.item_ids)
                scores = runtime.score(history, candidate_ids)
                candidates = tuple(
                    ScoredCandidate(item_id, score, "controlled-cpu-dot-product")
                    for item_id, score in zip(candidate_ids, scores, strict=True)
                )
            else:
                candidates = ()
            return RankedBatch(context.binding, Strategy.DENSE, candidates,
                               "candidate_budget_truncated" if limited else None)
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
        work = asyncio.create_task(asyncio.to_thread(self._save, result))
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            await _drain(work)
            raise

    def _save(self, result: RecommendationResult) -> None:
        binding = result.binding
        with self._connect() as connection:
            cursor = connection.execute(
                """
                SELECT session_id, session_epoch, history_version, bundle_id,
                       exclusion_version, requested_strategy, actual_strategy,
                       fallback_reason, status, model_snapshot, execution_owner
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
            model = decode_model(row["model_snapshot"])
            if ((result.model_version, result.captured_at_ms)
                    != (model.model_version if model else None, model.timestamp_ms if model else None)):
                raise SnapshotMismatch("result model differs from accepted input")
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
            if row["execution_owner"] is not None:
                execution = self._execution(binding.request_id)
                if execution is None or execution.owner != row["execution_owner"]:
                    raise SnapshotMismatch("result has no matching execution owner")
                execution.assert_held()
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
    def __init__(self, backend: PostgresDemoBackend) -> None:
        self.backend = backend

    async def check(self) -> ReadinessReport:
        return await asyncio.to_thread(self._check)

    def _check(self) -> ReadinessReport:
        blockers: list[str] = []
        try:
            if self.backend.manager is not None:
                self.backend.manager.recover()
            with psycopg.connect(self.backend.database_url) as connection:
                cursor = connection.execute(
                    """
                    SELECT active_bundle_id, admission_open
                    FROM catalog_control WHERE singleton = 1
                    """
                )
                row = cursor.fetchone()
        except psycopg.Error:
            return ReadinessReport(("database_not_connected", "model_runtime_not_loaded"))
        if (row is None or self.backend.runtime is None
                or str(row[0]) != self.backend.runtime.bundle_id):
            blockers.append("model_runtime_not_loaded")
        if row is None or row[0] is None:
            blockers.append("catalog_bundle_not_loaded")
        if row is None or not row[1]:
            blockers.append("publication_barrier_closed")
        return ReadinessReport(tuple(blockers))
