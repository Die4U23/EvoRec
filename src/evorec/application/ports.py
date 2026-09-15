"""Adapters must implement these contracts; none implies a ready production backend."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from evorec.domain.models import (
    RankedBatch, ReadinessReport, RecommendationCommand, RecommendationResult, RequestContext,
)


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
