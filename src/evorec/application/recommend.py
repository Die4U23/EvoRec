"""A testable recommendation workflow; no public business route is wired yet."""

import asyncio

from evorec.application.ports import AdmissionPort, RankingPort, ResultRecorderPort
from evorec.domain.errors import HistoryConflict, SnapshotMismatch, UnreportedFallback
from evorec.domain.models import RecommendationCommand, RecommendationResult, Strategy
from evorec.domain.recommendation import select_results


class Recommend:
    def __init__(self, admission: AdmissionPort, ranking: RankingPort, recorder: ResultRecorderPort):
        self.admission = admission
        self.ranking = ranking
        self.recorder = recorder

    async def execute(self, command: RecommendationCommand) -> RecommendationResult:
        async with asyncio.timeout(command.timeout_seconds):
            async with self.admission.acquire(command) as context:
                if context.request_id != command.request_id or context.session.session_id != command.session_id:
                    raise SnapshotMismatch("admission returned a different request or session")
                if context.session.history_version != command.expected_history_version:
                    raise HistoryConflict("history changed before admission")

                batch = await self.ranking.rank(context, command)
                if batch.binding != context.binding:
                    raise SnapshotMismatch("ranking result does not match the admitted snapshot")
                if (
                    command.strategy != Strategy.ADAPTIVE
                    and batch.actual_strategy != command.strategy
                    and batch.fallback_reason is None
                ):
                    raise UnreportedFallback("a fixed strategy changed without a fallback reason")

                result = RecommendationResult(
                    binding=context.binding,
                    requested_strategy=command.strategy,
                    actual_strategy=batch.actual_strategy,
                    items=select_results(batch.candidates, context, command.k),
                    fallback_reason=batch.fallback_reason,
                )
                await self.recorder.save(result)
                return result
