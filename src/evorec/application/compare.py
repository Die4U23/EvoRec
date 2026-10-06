"""Fixed-snapshot comparisons with optional immutable result persistence."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from time import perf_counter
from uuid import UUID

from evorec.application.ports import ComparisonRecordPort, ComparisonSnapshotPort, RankingPort
from evorec.domain.errors import (
    ComparisonStorageUnavailable, HistoryConflict, SnapshotMismatch, UnreportedFallback,
)
from evorec.domain.models import (
    RecommendationCommand, RequestBinding, RequestContext, ScoredCandidate, Strategy,
)
from evorec.domain.recommendation import select_results


@dataclass(frozen=True)
class ComparisonCommand:
    comparison_id: UUID
    session_id: UUID
    session_token: str
    expected_history_version: int
    strategies: tuple[Strategy, ...]
    k: int = 10
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        strategies = tuple(Strategy(strategy) for strategy in self.strategies)
        if not 2 <= len(strategies) <= 3 or len(set(strategies)) != len(strategies):
            raise ValueError("comparison requires two or three distinct strategies")
        if not set(strategies) <= {Strategy.POPULAR, Strategy.DENSE, Strategy.ADAPTIVE}:
            raise ValueError("only currently exposed local strategies can be previewed")
        # HTTP comparisons remain 1..10; offline evaluation can request up to 50
        # from the same ranking port without pretending a Top-10 contains Recall@20.
        if type(self.k) is not int or not 1 <= self.k <= 50:
            raise ValueError("comparison k must be between 1 and 50")
        if type(self.expected_history_version) is not int or self.expected_history_version < 0:
            raise ValueError("history version must be non-negative")
        if not self.session_token or not self.session_token.strip():
            raise ValueError("session token is required")
        if (isinstance(self.timeout_seconds, bool) or not isfinite(self.timeout_seconds)
                or self.timeout_seconds <= 0):
            raise ValueError("timeout must be finite and positive")
        object.__setattr__(self, "strategies", strategies)

    @property
    def input_sha256(self) -> str:
        payload = {"session_id": str(self.session_id), "history_version": self.expected_history_version,
                   "strategies": [strategy.value for strategy in self.strategies], "k": self.k}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ComparedStrategy:
    requested_strategy: Strategy
    actual_strategy: Strategy
    fallback_reason: str | None
    elapsed_ms: float
    items: tuple[ScoredCandidate, ...]
    unique_item_ids: tuple[str, ...]


@dataclass(frozen=True)
class ComparisonPreview:
    context: RequestContext
    snapshot_at: datetime
    common_item_ids: tuple[str, ...]
    strategies: tuple[ComparedStrategy, ...]
    requested_k: int

    @property
    def binding(self) -> RequestBinding:
        return self.context.binding


@dataclass(frozen=True)
class ComparisonSummary:
    comparison_id: UUID
    session_id: UUID
    history_version: int
    bundle_id: UUID
    snapshot_at: datetime
    requested_k: int
    requested_strategies: tuple[Strategy, ...]
    actual_strategies: tuple[Strategy, ...]


@dataclass(frozen=True)
class ComparisonPage:
    items: tuple[ComparisonSummary, ...]
    offset: int
    limit: int
    has_more: bool


class CompareStrategies:
    def __init__(self, snapshots: ComparisonSnapshotPort, ranking: RankingPort,
                 records: ComparisonRecordPort | None = None):
        self.snapshots = snapshots
        self.ranking = ranking
        self.records = records

    async def save(self, command: ComparisonCommand) -> ComparisonPreview:
        if self.records is None:
            raise ComparisonStorageUnavailable("saved comparisons require PostgreSQL")
        async with asyncio.timeout(command.timeout_seconds):
            stored = await self.records.find(command)
            if stored is not None:
                return stored
            result = await self.preview(command)
            return await self.records.save(command, result)

    async def get(self, comparison_id: UUID, session_id: UUID,
                  session_token: str) -> ComparisonPreview:
        if self.records is None:
            raise ComparisonStorageUnavailable("saved comparisons require PostgreSQL")
        return await self.records.get(comparison_id, session_id, session_token)

    async def list(self, session_id: UUID, session_token: str,
                   offset: int = 0, limit: int = 20) -> ComparisonPage:
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("comparison offset must be between 0 and 10000")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("comparison limit must be between 1 and 50")
        if self.records is None:
            raise ComparisonStorageUnavailable("saved comparisons require PostgreSQL")
        rows = await self.records.list(session_id, session_token, offset, limit + 1)
        return ComparisonPage(rows[:limit], offset, limit, len(rows) > limit)

    async def preview(self, command: ComparisonCommand) -> ComparisonPreview:
        async with asyncio.timeout(command.timeout_seconds):
            snapshot = await self.capture(command)
            return await self.execute(command, snapshot)

    async def capture(self, command: ComparisonCommand) -> ComparisonPreview:
        first = RecommendationCommand(
            command.comparison_id, command.session_id, command.session_token,
            command.expected_history_version, command.strategies[0], command.k,
            command.timeout_seconds,
        )
        async with asyncio.timeout(command.timeout_seconds):
            context = await self.snapshots.snapshot_for_comparison(first)
            if context.request_id != command.comparison_id or context.session.session_id != command.session_id:
                raise SnapshotMismatch("comparison snapshot belongs to a different request or session")
            if context.session.history_version != command.expected_history_version:
                raise HistoryConflict("history changed before comparison")
            captured_at = (datetime.fromtimestamp(context.model.timestamp_ms / 1000, timezone.utc)
                           if context.model else datetime.now(timezone.utc))
            return ComparisonPreview(context, captured_at, (), (), command.k)

    async def execute(self, command: ComparisonCommand, snapshot: ComparisonPreview,
                      progress: Callable[[int], Awaitable[None]] | None = None) -> ComparisonPreview:
        context = snapshot.context
        if (context.request_id != command.comparison_id or context.session.session_id != command.session_id
                or context.session.history_version != command.expected_history_version
                or snapshot.requested_k != command.k):
            raise SnapshotMismatch("frozen comparison differs from its command")
        async with asyncio.timeout(command.timeout_seconds):
            ranked = []
            for strategy in command.strategies:
                request = RecommendationCommand(
                    command.comparison_id, command.session_id, command.session_token,
                    command.expected_history_version, strategy, command.k, command.timeout_seconds,
                )
                started = perf_counter()
                batch = await self.ranking.rank(context, request)
                if batch.binding != context.binding:
                    raise SnapshotMismatch("comparison ranking changed the snapshot binding")
                if batch.model_version != (context.model.model_version if context.model else None):
                    raise SnapshotMismatch("comparison ranking changed the frozen model")
                if (strategy != Strategy.ADAPTIVE and batch.actual_strategy != strategy
                        and batch.fallback_reason is None):
                    raise UnreportedFallback("a fixed strategy changed without a fallback reason")
                items = select_results(batch.candidates, context, command.k)
                elapsed_ms = (perf_counter() - started) * 1000
                ranked.append((strategy, batch, items, elapsed_ms))
                if progress is not None:
                    await progress(len(ranked))

        item_sets = [set(item.item_id for item in items) for _, _, items, _ in ranked]
        common = tuple(sorted(set.intersection(*item_sets)))
        entries = []
        for index, (strategy, batch, items, elapsed_ms) in enumerate(ranked):
            other_items = set().union(*(items_set for position, items_set in enumerate(item_sets)
                                        if position != index))
            entries.append(ComparedStrategy(
                strategy, batch.actual_strategy, batch.fallback_reason, elapsed_ms, items,
                tuple(item.item_id for item in items if item.item_id not in other_items),
            ))
        return ComparisonPreview(context, snapshot.snapshot_at, common, tuple(entries), command.k)
