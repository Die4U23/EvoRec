import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from evorec.application.recommend import Recommend
from evorec.domain.errors import HistoryConflict, SnapshotMismatch, UnreportedFallback
from evorec.domain.models import (
    CatalogSnapshot, RankedBatch, RecommendationCommand, RequestContext,
    ScoredCandidate, SessionSnapshot, Strategy,
)
from evorec.domain.recommendation import select_results


@pytest.fixture
def context():
    return RequestContext(
        uuid4(), SessionSnapshot(uuid4(), 2, 7, ("seen",), frozenset({"hidden"})),
        CatalogSnapshot(uuid4(), 3, frozenset({"seen", "hidden", "a", "b", "c"})),
    )


def command_for(context, **changes):
    command = RecommendationCommand(
        context.request_id, context.session.session_id, "test-token", 7, Strategy.DENSE, 10, 1.0,
    )
    return replace(command, **changes)


class MemoryAdmission:
    """Test double, never connected to the public application."""

    def __init__(self, context, events):
        self.context = context
        self.events = events

    @asynccontextmanager
    async def acquire(self, command):
        self.events.append("acquire")
        try:
            yield self.context
        finally:
            self.events.append("release")


class MemoryRanking:
    def __init__(self, batch, events):
        self.batch = batch
        self.events = events

    async def rank(self, context, command):
        self.events.append("rank")
        return self.batch


class MemoryRecorder:
    def __init__(self, events):
        self.events = events
        self.results = []

    async def save(self, result):
        self.events.append("save")
        self.results.append(result)


def assemble(context, batch=None):
    events = []
    batch = batch or RankedBatch(context.binding, Strategy.DENSE, (ScoredCandidate("a", 0.8, "dense"),))
    recorder = MemoryRecorder(events)
    use_case = Recommend(MemoryAdmission(context, events), MemoryRanking(batch, events), recorder)
    return use_case, recorder, events


def test_filtering_deduplication_order_and_no_padding(context):
    candidates = (
        ScoredCandidate("seen", 100, "dense"), ScoredCandidate("hidden", 100, "dense"),
        ScoredCandidate("deleted", 100, "dense"), ScoredCandidate("unknown", 100, "dense"),
        ScoredCandidate("a", 0.4, "dense"), ScoredCandidate("a", 0.8, "z-source"),
        ScoredCandidate("b", 0.8, "dense"), ScoredCandidate("a", 0.8, "a-source"),
        ScoredCandidate("c", -0.1, "dense"),
    )
    result = select_results(candidates, context, 10)
    assert [(item.item_id, item.score, item.source) for item in result] == [
        ("a", 0.8, "a-source"), ("b", 0.8, "dense"), ("c", -0.1, "dense"),
    ]
    assert select_results(tuple(reversed(candidates)), context, 10) == result
    assert select_results(candidates, context, 1) == result[:1]
    assert select_results((), context, 10) == ()
    assert select_results(candidates, replace(context, catalog=replace(context.catalog, eligible_items=frozenset())), 10) == ()


def test_snapshots_are_copied_from_mutable_inputs_and_cannot_be_reassigned(context):
    history, hidden, eligible = ["seen"], {"hidden"}, {"a"}
    session = SessionSnapshot(context.session.session_id, 2, 7, history, hidden)
    catalog = CatalogSnapshot(context.catalog.bundle_id, 3, eligible)
    history.append("later")
    hidden.add("a")
    eligible.clear()
    assert session.history == ("seen",)
    assert session.hidden_items == frozenset({"hidden"})
    assert catalog.eligible_items == frozenset({"a"})
    with pytest.raises(FrozenInstanceError):
        catalog.exclusion_version = 4


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), True])
def test_non_finite_or_boolean_scores_are_rejected(score):
    with pytest.raises(ValueError):
        ScoredCandidate("a", score, "dense")


@pytest.mark.parametrize("changes", [
    {"k": 0}, {"k": 51}, {"k": True}, {"expected_history_version": -1},
    {"expected_history_version": True}, {"timeout_seconds": 0},
    {"timeout_seconds": float("inf")}, {"timeout_seconds": float("nan")},
])
def test_invalid_execution_limits_are_rejected(context, changes):
    with pytest.raises(ValueError):
        command_for(context, **changes)


def test_result_is_recorded_before_release_and_return(context):
    use_case, recorder, events = assemble(context)
    result = asyncio.run(use_case.execute(command_for(context)))
    events.append("returned")
    assert events == ["acquire", "rank", "save", "release", "returned"]
    assert recorder.results == [result]
    assert result.binding == context.binding
    assert result.requested_strategy == result.actual_strategy == Strategy.DENSE


def test_stale_history_is_rejected_before_ranking(context):
    use_case, recorder, events = assemble(context)
    with pytest.raises(HistoryConflict):
        asyncio.run(use_case.execute(command_for(context, expected_history_version=6)))
    assert events == ["acquire", "release"]
    assert recorder.results == []


@pytest.mark.parametrize("field", ["request_id", "session_id"])
def test_admission_cannot_substitute_another_request_or_session(context, field):
    use_case, recorder, events = assemble(context)
    with pytest.raises(SnapshotMismatch):
        asyncio.run(use_case.execute(command_for(context, **{field: uuid4()})))
    assert events == ["acquire", "release"]
    assert recorder.results == []


@pytest.mark.parametrize("field", [
    "request_id", "session_id", "session_epoch", "history_version", "bundle_id", "exclusion_version",
])
def test_every_snapshot_binding_component_must_match(context, field):
    old = getattr(context.binding, field)
    changed = old + 1 if isinstance(old, int) else uuid4()
    batch = RankedBatch(replace(context.binding, **{field: changed}), Strategy.DENSE, ())
    use_case, recorder, events = assemble(context, batch)
    with pytest.raises(SnapshotMismatch):
        asyncio.run(use_case.execute(command_for(context)))
    assert events == ["acquire", "rank", "release"]
    assert recorder.results == []


def test_fixed_strategy_cannot_silently_fallback(context):
    batch = RankedBatch(context.binding, Strategy.POPULAR, ())
    use_case, recorder, events = assemble(context, batch)
    with pytest.raises(UnreportedFallback):
        asyncio.run(use_case.execute(command_for(context)))
    assert recorder.results == []
    assert events[-1] == "release"


def test_fallback_reason_and_actual_path_are_preserved(context):
    batch = RankedBatch(context.binding, Strategy.POPULAR, (), "dense_runtime_unavailable")
    use_case, _, _ = assemble(context, batch)
    result = asyncio.run(use_case.execute(command_for(context)))
    assert result.requested_strategy == Strategy.DENSE
    assert result.actual_strategy == Strategy.POPULAR
    assert result.fallback_reason == "dense_runtime_unavailable"


def test_adaptive_strategy_resolves_to_a_concrete_path(context):
    use_case, _, _ = assemble(context)
    result = asyncio.run(use_case.execute(command_for(context, strategy=Strategy.ADAPTIVE)))
    assert result.requested_strategy == Strategy.ADAPTIVE
    assert result.actual_strategy == Strategy.DENSE
    assert result.fallback_reason is None
    with pytest.raises(ValueError, match="resolve"):
        RankedBatch(context.binding, Strategy.ADAPTIVE, ())


def test_record_failure_prevents_success_and_releases_lease(context):
    use_case, _, events = assemble(context)

    class BrokenRecorder:
        async def save(self, result):
            events.append("save_failed")
            raise OSError("database unavailable")

    use_case.recorder = BrokenRecorder()
    with pytest.raises(OSError, match="database unavailable"):
        asyncio.run(use_case.execute(command_for(context)))
    assert events == ["acquire", "rank", "save_failed", "release"]


def test_timeout_cancels_cooperative_work_and_releases_local_lease(context):
    use_case, recorder, events = assemble(context)

    class WaitingRanking:
        async def rank(self, context, command):
            events.append("rank")
            try:
                await asyncio.Future()  # Wait until cancelled; no real model work.
            finally:
                events.append("rank_cancelled")

    use_case.ranking = WaitingRanking()
    with pytest.raises(TimeoutError):
        asyncio.run(use_case.execute(command_for(context, timeout_seconds=0.05)))
    assert events == ["acquire", "rank", "rank_cancelled", "release"]
    assert recorder.results == []


def test_runtime_failure_is_not_reported_as_empty_success(context):
    use_case, recorder, events = assemble(context)

    class BrokenRanking:
        async def rank(self, context, command):
            raise RuntimeError("runtime disconnected")

    use_case.ranking = BrokenRanking()
    with pytest.raises(RuntimeError, match="disconnected"):
        asyncio.run(use_case.execute(command_for(context)))
    assert recorder.results == []
    assert events == ["acquire", "release"]
