"""Process-local adapters for exercising the M1 recommendation workflow.

This module deliberately does not claim durable persistence. It remains the
zero-configuration fallback and deterministic test backend for the HTTP workflow.
"""

import asyncio
import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

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
)
from evorec.domain.models import (
    CatalogSnapshot,
    CreatedSession,
    FeedbackCommand,
    FeedbackKind,
    FeedbackResult,
    RankedBatch,
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


_DEMO_SCORES = {
    "demo-coop": 0.95,
    "demo-racing": 0.85,
    "demo-strategy": 0.75,
}
_DEMO_ITEMS = (
    {"item_id": "demo-coop", "title": "Co-op Demo", "category": "demo", "description": "合作体验示例商品", "image_url": None, "is_active": True},
    {"item_id": "demo-racing", "title": "Racing Demo", "category": "demo", "description": "竞速体验示例商品", "image_url": None, "is_active": True},
    {"item_id": "demo-strategy", "title": "Strategy Demo", "category": "demo", "description": "策略体验示例商品", "image_url": None, "is_active": True},
)


class InMemoryDemoBackend:
    """Short-lived session, admission, ranking, and result adapters.

    State is isolated per application instance and is lost when the process
    exits. A single lock makes admission, reset, and completion transitions
    deterministic for the local demonstration; it is not a database substitute.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[UUID, SessionSnapshot] = {}
        self._session_tokens: dict[UUID, str] = {}
        self._request_states: dict[UUID, str] = {}
        self._request_commands: dict[UUID, tuple[UUID, int, Strategy, int]] = {}
        self._request_bindings: dict[UUID, RequestBinding] = {}
        self._results: dict[UUID, RecommendationResult] = {}
        self._feedback: dict[UUID, tuple[str, FeedbackResult]] = {}
        self.catalog = CatalogSnapshot(
            bundle_id=uuid5(NAMESPACE_URL, "https://evorec.local/bundles/m1-memory-demo"),
            exclusion_version=0,
            eligible_items=frozenset(_DEMO_SCORES),
        )

    @staticmethod
    def _token_sha256(access_token: str) -> str:
        return hashlib.sha256(access_token.encode("utf-8")).hexdigest()

    async def create_session(self, profile_id: str = "new") -> CreatedSession:
        snapshot = SessionSnapshot(uuid4(), 0, 0, initial_history(profile_id), frozenset(), profile_id=profile_id)
        access_token = secrets.token_urlsafe(32)
        async with self._lock:
            self._sessions[snapshot.session_id] = snapshot
            self._session_tokens[snapshot.session_id] = self._token_sha256(access_token)
        return CreatedSession(snapshot, access_token)

    async def get_session(self, session_id: UUID, access_token: str) -> SessionSnapshot:
        async with self._lock:
            try:
                snapshot = self._sessions[session_id]
            except KeyError as exc:
                raise ResourceNotFound("session does not exist") from exc
            if not hmac.compare_digest(
                self._session_tokens[session_id], self._token_sha256(access_token)
            ):
                raise AccessDenied("session token is invalid")
            return snapshot

    async def reset_session(self, session_id: UUID, access_token: str) -> SessionSnapshot:
        async with self._lock:
            try:
                current = self._sessions[session_id]
            except KeyError as exc:
                raise ResourceNotFound("session does not exist") from exc
            if not hmac.compare_digest(
                self._session_tokens[session_id], self._token_sha256(access_token)
            ):
                raise AccessDenied("session token is invalid")
            reset = replace(
                current,
                epoch=current.epoch + 1,
                history_version=current.history_version + 1,
                history=initial_history(current.profile_id),
                hidden_items=frozenset(),
                favorite_items=frozenset(),
            )
            self._sessions[session_id] = reset
            return reset

    async def record_feedback(self, command: FeedbackCommand) -> FeedbackResult:
        async with self._lock:
            try:
                session = self._sessions[command.session_id]
            except KeyError as exc:
                raise ResourceNotFound("session does not exist") from exc
            if not hmac.compare_digest(
                self._session_tokens[command.session_id],
                self._token_sha256(command.session_token),
            ):
                raise AccessDenied("session token is invalid")

            previous = self._feedback.get(command.event_id)
            if previous is not None:
                if not hmac.compare_digest(previous[0], command.payload_sha256):
                    raise IdempotencyConflict("event ID was already used for different feedback")
                return replace(previous[1], replayed=True)

            result = self._results.get(command.request_id)
            if result is None or result.binding.session_id != command.session_id:
                raise FeedbackSourceMismatch("feedback request does not belong to this session")
            if not any(item.item_id == command.item_id for item in result.items):
                raise FeedbackSourceMismatch("feedback item was not returned by the request")
            if result.binding.session_epoch != session.epoch:
                raise SessionEpochConflict("feedback request belongs to an earlier session epoch")

            history = session.history
            hidden = session.hidden_items
            favorites = session.favorite_items
            changed = False
            if command.kind == FeedbackKind.DETAIL_VIEW:
                history = (*history, command.item_id)
                changed = True
            elif command.kind == FeedbackKind.HIDE_SET:
                updated = set(hidden)
                before = command.item_id in updated
                if command.desired_state:
                    updated.add(command.item_id)
                else:
                    updated.discard(command.item_id)
                hidden = frozenset(updated)
                changed = before != command.desired_state
            elif command.kind == FeedbackKind.FAVORITE_SET:
                updated = set(favorites)
                before = command.item_id in updated
                if command.desired_state:
                    updated.add(command.item_id)
                else:
                    updated.discard(command.item_id)
                favorites = frozenset(updated)
                changed = before != command.desired_state
                if changed and command.desired_state:
                    history = (*history, command.item_id)

            if changed:
                session = replace(
                    session,
                    history_version=session.history_version + 1,
                    history=history,
                    hidden_items=hidden,
                    favorite_items=favorites,
                )
                self._sessions[command.session_id] = session

            exposure_event_id = None
            if command.kind == FeedbackKind.DETAIL_VIEW:
                exposure_event_id = detail_exposure_event_id(command.event_id)
                exposure_hash = detail_exposure_payload_sha256(command.event_id)
                previous_exposure = self._feedback.get(exposure_event_id)
                if previous_exposure is not None and not hmac.compare_digest(
                    previous_exposure[0], exposure_hash
                ):
                    raise IdempotencyConflict(
                        "derived exposure event ID was already used by another event"
                    )
                self._feedback[exposure_event_id] = (
                    exposure_hash,
                    FeedbackResult(
                        exposure_event_id,
                        command.session_id,
                        session.epoch,
                        session.history_version,
                        False,
                    ),
                )
            feedback = FeedbackResult(
                command.event_id,
                command.session_id,
                session.epoch,
                session.history_version,
                False,
                exposure_event_id,
            )
            self._feedback[command.event_id] = (command.payload_sha256, feedback)
            return feedback

    def list_items(self) -> list[dict[str, object]]:
        return [dict(item) for item in _DEMO_ITEMS]

    def get_item(self, item_id: str) -> dict[str, object] | None:
        return next((dict(item) for item in _DEMO_ITEMS if item["item_id"] == item_id), None)

    @asynccontextmanager
    async def acquire(self, command: RecommendationCommand):
        async with self._lock:
            try:
                session = self._sessions[command.session_id]
            except KeyError as exc:
                raise ResourceNotFound("session does not exist") from exc
            if not hmac.compare_digest(
                self._session_tokens[command.session_id], self._token_sha256(command.session_token)
            ):
                raise AccessDenied("session token is invalid")
            if command.request_id in self._request_states:
                expected = (command.session_id, command.expected_history_version,
                            command.strategy, command.k)
                if self._request_commands[command.request_id] != expected:
                    raise IdempotencyConflict("recommendation key was used for different input")
                state = self._request_states[command.request_id]
                if state == "completed":
                    raise IdempotencyReplay(self._results[command.request_id])
                if state == "accepted":
                    raise IdempotencyInProgress("recommendation is still in progress")
                raise IdempotencyConflict("recommendation key belongs to a failed request")
            if session.history_version != command.expected_history_version:
                raise HistoryConflict("history changed before admission")
            context = RequestContext(command.request_id, session, self.catalog)
            self._request_states[command.request_id] = "accepted"
            self._request_commands[command.request_id] = (
                command.session_id, command.expected_history_version,
                command.strategy, command.k,
            )
            self._request_bindings[command.request_id] = context.binding

        try:
            yield context
        except BaseException:
            async with self._lock:
                if self._request_states.get(command.request_id) == "accepted":
                    self._request_states[command.request_id] = "failed"
            raise

    async def rank(self, context: RequestContext, command: RecommendationCommand) -> RankedBatch:
        fallback_reason = None
        actual_strategy = command.strategy
        if command.strategy == Strategy.ADAPTIVE:
            actual_strategy = Strategy.POPULAR
        elif command.strategy != Strategy.POPULAR:
            actual_strategy = Strategy.POPULAR
            fallback_reason = "strategy_not_loaded_in_memory_demo"
        candidates = tuple(
            ScoredCandidate(item_id, score, "memory-popular")
            for item_id, score in _DEMO_SCORES.items()
        )
        return RankedBatch(context.binding, actual_strategy, candidates, fallback_reason)

    async def save(self, result: RecommendationResult) -> None:
        request_id = result.binding.request_id
        async with self._lock:
            state = self._request_states.get(request_id)
            if state == "completed":
                if self._results[request_id] != result:
                    raise SnapshotMismatch("completed request has different result content")
                return
            if state != "accepted" or self._request_bindings.get(request_id) != result.binding:
                raise SnapshotMismatch("result does not match an accepted request")
            self._results[request_id] = result
            self._request_states[request_id] = "completed"
