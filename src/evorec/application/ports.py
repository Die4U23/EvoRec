"""Adapters must implement these contracts; none implies a ready production backend."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from evorec.domain.models import (
    CreatedSession,
    FeedbackCommand,
    FeedbackResult,
    RankedBatch,
    ReadinessReport,
    RecommendationCommand,
    RecommendationResult,
    RequestContext,
    SessionSnapshot,
)


class SessionPort(Protocol):
    async def create_session(self, profile_id: str = "new") -> CreatedSession:
        """Create a session and return its access token exactly once."""
        ...

    async def get_session(self, session_id: UUID, access_token: str) -> SessionSnapshot:
        """Return a session only when its ownership token matches."""
        ...

    async def reset_session(self, session_id: UUID, access_token: str) -> SessionSnapshot:
        """Atomically advance epoch and restore the selected profile's initial state."""
        ...


class FeedbackPort(Protocol):
    async def record_feedback(self, command: FeedbackCommand) -> FeedbackResult:
        """Record feedback atomically and replay the original outcome by event ID."""
        ...


class AdmissionPort(Protocol):
    def acquire(self, command: RecommendationCommand) -> AbstractAsyncContextManager[RequestContext]:
        """Validate access, save acceptance, bind a snapshot, and release its lease on exit.

        On abnormal exit, reconcile uncertain commits before finalizing failure;
        never overwrite a completed request. A remote job that outlives cancellation
        must independently retain its own model/index lease.
        """
        ...


class RankingPort(Protocol):
    async def rank(self, context: RequestContext, command: RecommendationCommand) -> RankedBatch:
        """Return comparable finite scores, exact binding, and the real execution path.

        Queue limits and cancellation of remote work belong to the adapter/runtime.
        Raw scores from unrelated retrieval methods cannot simply be concatenated.
        """
        ...


class ResultRecorderPort(Protocol):
    async def save(self, result: RecommendationResult) -> None:
        """Persist before success; request_id is the idempotency key.

        A timeout during commit is an uncertain outcome requiring later reconciliation.
        """
        ...


class ReadinessPort(Protocol):
    async def check(self) -> ReadinessReport:
        """Inspect the configured business dependencies, including serving availability."""
        ...


class DemoBackendPort(
    SessionPort,
    FeedbackPort,
    AdmissionPort,
    RankingPort,
    ResultRecorderPort,
    Protocol,
):
    """Combined local-demo boundary used only by the composition root."""
