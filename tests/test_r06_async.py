"""Real frozen-package numerics and deterministic cross-loop CPU lifecycle checks."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import gc
import struct
from threading import Event
import weakref

import pytest

from evorec.domain.errors import ManagementError, SnapshotMismatch
from evorec.domain.models import RecommendationCommand, Strategy
from evorec.infrastructure.r06_async import R06CPUQueue, R06RankingPort
from evorec.infrastructure.r06_bundle import load_r06_bundle
from evorec.infrastructure.residual_ranker import ControlledLoadError
from test_r06_bundle import _build, _records
from test_r06_serving import _request


@pytest.fixture
def frozen(tmp_path):
    root, target, digest = _build(tmp_path)
    return load_r06_bundle(root, target, expected_manifest_sha256=digest)


def _port(queue, bundle, request=None):
    request = request or _request(bundle.adapter, eligible={"c", "d", "e", "zero"})
    return R06RankingPort(queue, bundle, request.context, timestamp_ms=request.timestamp_ms,
                          full_seen=request.full_seen, catalog_items=_records(bundle, request.context.catalog.eligible_items))


def _command(port, **changes):
    context = port.request.context
    command = RecommendationCommand(context.request_id, context.session.session_id, "synthetic-token",
                                    context.session.history_version, Strategy.DENSE, 3, 5.)
    return replace(command, **changes)


async def _started(event):
    assert await asyncio.to_thread(event.wait, 3), "CPU fixture did not start"


async def _settle(predicate):
    async with asyncio.timeout(3):
        while not predicate(): await asyncio.sleep(.001)


def test_ranking_port_independent_scores_binding_and_frozen_inputs(frozen):
    async def run():
        queue = R06CPUQueue()
        try:
            port = _port(queue, frozen)
            batch = await port.rank(port.request.context, _command(port))
            assert batch.binding == port.request.context.binding
            assert batch.actual_strategy == Strategy.DENSE and batch.fallback_reason is None
            assert all(c.source == "r06-a-frozen-s17" for c in batch.candidates)
            f32 = lambda n: struct.unpack("<f", struct.pack("<f", n))[0]
            cf = {item: f32(61/(60+i)) for i,item in enumerate(("zero", "c", "d"), 1)}
            content = {item: f32(61/(60+i)) for i,item in enumerate(("e", "c", "d", "zero"), 1)}
            assert {c.item_id:c.score for c in batch.candidates} == {
                item:f32(4*(cf.get(item, 0.)+content[item])) for item in content}
            assert port.model_version == frozen.model_version
            with pytest.raises(FrozenInstanceError): port.bundle = None
            assert queue.outstanding == 0
        finally: await queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["history", "eligible", "request_id", "session_id", "version", "strategy"])
def test_port_rejects_context_or_command_changes_before_cpu_submission(frozen, change):
    from uuid import uuid4
    async def run():
        queue = R06CPUQueue()
        try:
            port = _port(queue, frozen)
            context, command = port.request.context, _command(port)
            if change == "history": context = replace(context, session=replace(context.session, history=("b",)))
            elif change == "eligible": context = replace(context, catalog=replace(context.catalog, eligible_items=frozenset()))
            elif change == "request_id": command = replace(command, request_id=uuid4())
            elif change == "session_id": command = replace(command, session_id=uuid4())
            elif change == "version": command = replace(command, expected_history_version=99)
            else: command = replace(command, strategy=Strategy.GENERATIVE)
            with pytest.raises((SnapshotMismatch, ManagementError)): await port.rank(context, command)
            assert queue.outstanding == 0
        finally: await queue.aclose()
    asyncio.run(run())


def test_capture_rejects_actual_text_drift_and_copies_mutable_seen(frozen):
    queue = R06CPUQueue()
    try:
        request = _request(frozen.adapter)
        records = list(_records(frozen, request.context.catalog.eligible_items))
        seen = set(request.full_seen)
        port = R06RankingPort(queue, frozen, request.context, timestamp_ms=11, full_seen=seen, catalog_items=records)
        seen.add("b"); records.clear()
        assert port.request.full_seen == request.full_seen
        changed = list(_records(frozen, request.context.catalog.eligible_items))
        changed[0] = replace(changed[0], text="modified")
        with pytest.raises(ControlledLoadError):
            R06RankingPort(queue, frozen, request.context, timestamp_ms=11, full_seen=seen, catalog_items=changed)
    finally: queue.close()


@pytest.mark.parametrize("workers,queued", [(True, 0), (0, 1), (5, 1), (1., 1), (1, True), (1, -1), (1, 33)])
def test_invalid_cpu_capacity_rejected(workers, queued):
    with pytest.raises(ValueError): R06CPUQueue(workers=workers, queued=queued)


def test_bounded_queue_cancel_queued_job_frees_slot_without_running():
    async def run():
        queue = R06CPUQueue(workers=1, queued=1)
        entered, release = Event(), Event()
        ran = []
        def block(): entered.set(); assert release.wait(3); return "running"
        running = asyncio.create_task(queue.run(block))
        try:
            await _started(entered)
            queued = asyncio.create_task(queue.run(lambda: ran.append("cancelled")))
            await _settle(lambda: queue.outstanding == 2)
            with pytest.raises(ManagementError) as error: await queue.run(lambda: 1)
            assert (error.value.code, error.value.status_code) == ("r06_queue_full", 429)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError): await queued
            assert queue.outstanding == 1 and ran == []
            replacement = asyncio.create_task(queue.run(lambda: "replacement"))
            await _settle(lambda: queue.outstanding == 2)
            release.set()
            assert await running == "running" and await replacement == "replacement"
        finally: release.set(); await queue.aclose()
    asyncio.run(run())


def test_running_and_repeated_cancellation_drain_cpu_before_acknowledgement():
    async def run():
        queue = R06CPUQueue(workers=1, queued=0)
        entered, release, exited = Event(), Event(), Event()
        def block():
            entered.set(); assert release.wait(3); exited.set(); raise ValueError("discarded CPU failure")
        task = asyncio.create_task(queue.run(block))
        try:
            await _started(entered)
            for _ in range(3):
                task.cancel(); await asyncio.sleep(.005)
                assert not task.done() and not exited.is_set() and queue.outstanding == 1
            with pytest.raises(ManagementError): await queue.run(lambda: 1)
            release.set()
            with pytest.raises(asyncio.CancelledError): await task
            assert exited.is_set() and queue.outstanding == 0
            assert await queue.run(lambda: 7) == 7
        finally: release.set(); await queue.aclose()
    asyncio.run(run())


def test_event_loop_remains_responsive_and_timeout_drains_real_work():
    async def run():
        queue = R06CPUQueue()
        entered, release, exited = Event(), Event(), Event()
        def block(): entered.set(); assert release.wait(3); exited.set(); return 9
        async def timed():
            async with asyncio.timeout(.03): return await queue.run(block)
        task = asyncio.create_task(timed())
        try:
            await _started(entered)
            await asyncio.sleep(.06)
            assert not task.done() and not exited.is_set()
            release.set()
            with pytest.raises(TimeoutError): await task
            assert exited.is_set()
        finally: release.set(); await queue.aclose()
    asyncio.run(run())


def test_close_rejects_new_work_cancels_queue_and_drains_despite_repeated_cancel():
    async def run():
        queue = R06CPUQueue(workers=1, queued=1)
        entered, release = Event(), Event()
        def block(): entered.set(); assert release.wait(3); return 1
        running = asyncio.create_task(queue.run(block))
        try:
            await _started(entered)
            queued = asyncio.create_task(queue.run(lambda: pytest.fail("closed queued job executed")))
            await _settle(lambda: queue.outstanding == 2)
            closing = asyncio.create_task(queue.aclose())
            with pytest.raises(asyncio.CancelledError): await queued
            for _ in range(2):
                closing.cancel(); await asyncio.sleep(.005)
                assert not closing.done() and queue.outstanding == 1
            with pytest.raises(ManagementError) as error: await queue.run(lambda: 1)
            assert (error.value.code, error.value.status_code) == ("r06_queue_closed", 503)
            release.set(); assert await running == 1
            with pytest.raises(asyncio.CancelledError): await closing
            await queue.aclose(); assert queue.outstanding == 0
        finally: release.set(); await queue.aclose()
    asyncio.run(run())


def test_one_pool_is_safe_across_concurrent_and_sequential_event_loops():
    queue = R06CPUQueue(workers=2, queued=32)
    def loop(index):
        async def run(): return await asyncio.gather(*(queue.run(lambda n=n: (index, n)) for n in range(8)))
        return asyncio.run(run())
    try:
        with ThreadPoolExecutor(max_workers=3) as callers: results = list(callers.map(loop, range(3)))
        assert results == [[(i,n) for n in range(8)] for i in range(3)]
        assert loop(7) == [(7,n) for n in range(8)] and queue.outstanding == 0
    finally: asyncio.run(queue.aclose())


def test_fast_completions_and_failures_release_capacity():
    async def run():
        queue = R06CPUQueue(workers=1, queued=0)
        try:
            def fail(): raise ValueError("synthetic failure")
            with pytest.raises(ValueError): await queue.run(fail)
            for value in range(100): assert await queue.run(lambda: value) == value
            assert queue.outstanding == 0
        finally: await queue.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["binding", "strategy", "version", "timestamp"])
def test_actual_cpu_result_mismatch_rejected_without_fallback(frozen, monkeypatch, change):
    from uuid import uuid4
    async def run():
        queue = R06CPUQueue()
        try:
            port = _port(queue, frozen)
            original = type(frozen.adapter).score
            def changed(adapter, request):
                result = original(adapter, request)
                if change == "binding": return replace(result, batch=replace(result.batch, binding=replace(result.batch.binding, request_id=uuid4())))
                if change == "strategy": return replace(result, batch=replace(result.batch, actual_strategy=Strategy.POPULAR))
                if change == "version": return replace(result, model_version="b"*64)
                return replace(result, timestamp_ms=12)
            monkeypatch.setattr(type(frozen.adapter), "score", changed)
            with pytest.raises(SnapshotMismatch): await port.rank(port.request.context, _command(port))
            assert queue.outstanding == 0
        finally: await queue.aclose()
    asyncio.run(run())


def test_captured_old_bundle_stays_alive_through_cancelled_cpu(frozen, monkeypatch):
    async def run():
        queue = R06CPUQueue()
        old = replace(frozen, manifest_sha256="b"*64)
        port = _port(queue, old)
        captured = weakref.ref(old)
        entered, release = Event(), Event()
        original = type(old.adapter).score
        def block(adapter, request): entered.set(); assert release.wait(3); return original(adapter, request)
        with monkeypatch.context() as scope:
            scope.setattr(type(old.adapter), "score", block)
            task = asyncio.create_task(port.rank(port.request.context, _command(port)))
            del old, port
            try:
                await _started(entered)
                task.cancel(); await asyncio.sleep(.005); gc.collect()
                assert captured() is not None and not task.done()
                release.set()
                with pytest.raises(asyncio.CancelledError): await task
            finally: release.set(); await queue.aclose()
        del task
        # asyncio/concurrent-future notification handles may retain the completed
        # coroutine until subsequent loop turns; wait for those handles to drain.
        await _settle(lambda: (gc.collect(), captured())[1] is None)
        assert captured() is None  # Object lease, not a database/index eviction protocol.
    asyncio.run(run())


def test_unavailable_executor_fails_closed_without_capacity_leak():
    async def run():
        queue = R06CPUQueue()
        queue._executor.shutdown()
        with pytest.raises(ManagementError) as error: await queue.run(lambda: 1)
        assert (error.value.code, error.value.status_code) == ("r06_queue_unavailable", 503)
        assert queue.outstanding == 0
        await queue.aclose()
    asyncio.run(run())


def test_distinct_approved_bundles_and_histories_do_not_share_request_state(frozen, tmp_path):
    second = tmp_path / "second"
    second.mkdir()
    root, target, digest = _build(second)
    other = load_r06_bundle(root, target, expected_manifest_sha256=digest)
    async def run():
        queue = R06CPUQueue(workers=2)
        try:
            a = _port(queue, frozen)
            b = _port(queue, other, _request(other.adapter, history=("b",)))
            assert a.model_version != b.model_version
            results = await asyncio.gather(a.rank(a.request.context, _command(a)), b.rank(b.request.context, _command(b)))
            assert results[0].binding == a.request.context.binding
            assert results[1].binding == b.request.context.binding
            assert results[0].binding != results[1].binding
            for port,batch in zip((a,b), results, strict=True):
                assert batch == port.bundle.score(port.request.context, 11, port.request.full_seen,
                                                 _records(port.bundle, port.request.context.catalog.eligible_items)).batch
        finally: await queue.aclose()
    asyncio.run(run())


@pytest.fixture
def async_replay(frozen, tmp_path, monkeypatch):
    from scripts import verify_r06_async as script
    from test_r06_retrieval_runtime import _samples
    project = tmp_path / "synthetic-project"
    source = project / "scripts/verify.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic replay source")
    monkeypatch.setattr(script, "__file__", str(source))
    monkeypatch.setattr(script, "SOURCE_FILES", ("scripts/verify.py",))
    monkeypatch.setattr(script, "_source", lambda _: "a"*40)
    f32 = lambda n: struct.unpack("<f", struct.pack("<f", n))[0]
    samples, references = [], []
    for sample in _samples():
        ids = sorted(set(sample["collaborative"]) | set(sample["content"]))
        cf = {item:f32(61/(60+i)) for i,item in enumerate(sample["collaborative"],1)}
        content = {item:f32(61/(60+i)) for i,item in enumerate(sample["content"],1)}
        scores = [f32(4*(cf.get(item,0.)+content.get(item,0.))) for item in ids]
        samples.append({**sample, "expected_items":ids})
        references.append(dict(expected_scores=scores,expected_top20=sorted(range(len(ids)),key=lambda i:-scores[i])))
    monkeypatch.setattr(script, "_validation", lambda root,digest: samples if root.name=="features" else references)
    output = project / "artifacts/replay"
    def run():
        return script.verify(output,tmp_path / "managed",tmp_path / "managed" / str(frozen.bundle_id),frozen.manifest_sha256)
    return script, output, run


def test_async_replay_validates_actual_batches_against_independent_references(async_replay):
    import json
    _,output,run = async_replay
    result = run()
    assert result["actual_ranking_port_batches_exact"] and result["legal_top20_exact"]
    assert result["ranking_jobs_drained"] and result["activated"] is False
    assert [row["candidate_count"] for row in result["rows"]] == [5,5]
    assert json.loads((output / "verification.json").read_bytes()) == result


@pytest.mark.parametrize("failure", ["score", "source", "write", "interrupt", "rival"])
def test_async_replay_faults_never_publish_a_passed_report(async_replay, frozen, monkeypatch, failure):
    from pathlib import Path
    script,output,run = async_replay
    report = output / "verification.json"
    if failure == "score":
        original = type(frozen.adapter).score
        def changed(adapter,request):
            result = original(adapter,request)
            candidates = (replace(result.batch.candidates[0],score=result.batch.candidates[0].score+1),*result.batch.candidates[1:])
            return replace(result,batch=replace(result.batch,candidates=candidates))
        monkeypatch.setattr(type(frozen.adapter), "score", changed)
    elif failure == "source":
        calls = iter(["a"*40,"b"*40])
        monkeypatch.setattr(script, "_source", lambda _: next(calls))
    else:
        original = Path.open
        class Broken:
            def __enter__(self):
                self.stream = original(report,"xb"); return self
            def __exit__(self,*a): self.stream.close()
            def write(self,raw):
                self.stream.write(raw[:20])
                raise KeyboardInterrupt() if failure=="interrupt" else OSError("synthetic write failure")
        def fault(path,mode="r",*a,**k):
            if path==report and mode=="xb":
                if failure=="rival":
                    with original(report,"wb") as stream: stream.write(b"rival proof")
                else: return Broken()
            return original(path,mode,*a,**k)
        monkeypatch.setattr(Path, "open", fault)
    with pytest.raises((ValueError,OSError,KeyboardInterrupt)): run()
    assert (output / "source/scripts/verify.py").exists()
    if failure=="rival": assert report.read_bytes()==b"rival proof"
    else: assert not report.exists()


def test_async_replay_dirty_source_and_older_evidence_protected(async_replay, monkeypatch):
    script,output,run = async_replay
    def dirty(_): raise ControlledLoadError("source_dirty","synthetic dirty source")
    monkeypatch.setattr(script,"_source",dirty)
    with pytest.raises(ControlledLoadError): run()
    assert not output.exists()
    output.mkdir(parents=True)
    report = output / "verification.json"
    report.write_bytes(b"older proof")
    with pytest.raises(FileExistsError): run()
    assert report.read_bytes()==b"older proof"
