"""Diagnostic-only gate: actual work excludes waiting and unmarked calls bypass it."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event, Lock
from time import perf_counter

import pytest

from scripts import profile_r06_tcp, r06_service_lab
from scripts.r06_process_trace import ConcurrentTimings, trace_api
from evorec.infrastructure.postgres import PostgresDemoBackend
from evorec.infrastructure.r06_async import R06CPUQueue
from evorec.infrastructure.r06_serving import R06SnapshotRanker


@pytest.mark.parametrize("bad", [0, 1, "true", None])
def test_gate_requires_exact_bool(bad):
    with pytest.raises(ValueError, match="phase_gate"):
        ConcurrentTimings(2, phase_gate=bad)


@pytest.mark.parametrize("kwargs", [dict(phase_gate=True), dict(phase_gate=1), dict(phase_gate="true")])
def test_lab_rejects_gate_without_trace_before_validation(monkeypatch, kwargs):
    monkeypatch.setattr(r06_service_lab, "validate", lambda *args: pytest.fail("validation must not run"))
    with pytest.raises(ValueError, match="phase_gate"):
        r06_service_lab.R06ServiceLab(None, "PRIVATE", None, None, None, **kwargs)
    with pytest.raises(ValueError, match="phase_gate"):
        r06_service_lab.child(None, "PRIVATE", **kwargs)


@pytest.mark.parametrize("bad", [1, "true", None])
def test_profiler_rejects_gate_before_source_or_database(monkeypatch, bad):
    monkeypatch.setattr(profile_r06_tcp, "_source", lambda *args: pytest.fail("source must not run"))
    with pytest.raises(ValueError, match="phase_gate"):
        profile_r06_tcp.profile(None, "PRIVATE", None, None, None, phase_gate=bad)


@pytest.mark.parametrize("phase_gate", [False, True])
@pytest.mark.parametrize("second_label", ["retrieval_and_ranking", "actual_catalog_read_and_capture"])
def test_gate_serializes_marked_targets_only_and_excludes_wait(phase_gate, second_label):
    timing = ConcurrentTimings(2, phase_gate=phase_gate)
    entered, release, second_entered, attempted = Event(), Event(), Event(), Event()
    real_lock = Lock()

    class ObservedLock:
        def __enter__(self):
            if entered.is_set():
                attempted.set()
            real_lock.acquire()

        def __exit__(self, *args):
            real_lock.release()

    timing._phase_gate_lock = ObservedLock()
    requests = [dict(sample=i, started=perf_counter(), stages=[]) for i in range(2)]
    sentinel = object()

    def first():
        entered.set()
        assert release.wait(5)
        return sentinel

    def second():
        second_entered.set()
        return sentinel

    def invoke(index, label, original):
        token = timing.active.set(requests[index])
        try:
            result = timing.sync(label, original)()
            assert timing.phase.get() == "other"
            return result
        finally:
            timing.active.reset(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(invoke, 0, "actual_catalog_read_and_capture", first)
        try:
            assert entered.wait(5)
            # Unmarked startup calls and unrelated phases must not acquire the gate.
            assert timing.sync(second_label, lambda: sentinel)() is sentinel
            assert invoke(1, "result_write", lambda: sentinel) is sentinel
            second_future = pool.submit(invoke, 1, second_label, second)
            if phase_gate:
                assert attempted.wait(5)
                assert not second_entered.is_set()
            else:
                assert second_entered.wait(5)
        finally:
            release.set()
        assert first_future.result(5) is sentinel and second_future.result(5) is sentinel
    for request in requests:
        waits = [s for s in request["stages"] if s["stage"].endswith("_diagnostic_gate_wait")]
        assert len(waits) == int(phase_gate)
        if waits:
            wait, = waits
            work, = [s for s in request["stages"] if s["stage"] == wait["stage"].removesuffix("_diagnostic_gate_wait")]
            assert wait["thread_cpu_seconds"] is None and wait["wall_seconds"] >= 0
            assert work["start_seconds"] >= wait["start_seconds"] + wait["wall_seconds"]
    assert timing.current is None


def test_gate_releases_after_original_exception_and_restores_real_patches(monkeypatch):
    sentinel, failure = object(), RuntimeError("PRIVATE-ERROR")
    calls = []

    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        raise failure

    def score(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(PostgresDemoBackend, "_capture_context", capture)
    monkeypatch.setattr(R06SnapshotRanker, "score", score)
    timing = ConcurrentTimings(1, phase_gate=True)
    request = dict(sample=0, started=perf_counter(), stages=[])
    token = timing.active.set(request)
    try:
        with trace_api(timing, gc_events=False):
            with pytest.raises(RuntimeError) as caught:
                PostgresDemoBackend._capture_context("PRIVATE-ARG", value=sentinel)
            assert caught.value is failure and timing.phase.get() == "other"
            assert R06SnapshotRanker.score("PRIVATE-ARG", value=sentinel) is sentinel
    finally:
        timing.active.reset(token)
    assert PostgresDemoBackend._capture_context is capture and R06SnapshotRanker.score is score
    assert calls == [(("PRIVATE-ARG",), dict(value=sentinel))] * 2
    assert "PRIVATE" not in json.dumps(request["stages"])
    assert next(s for s in request["stages"] if s["stage"] == "actual_catalog_read_and_capture")["error_type"] == "RuntimeError"


def test_cancellation_while_gate_waiting_still_drains_real_cpu_work():
    timing = ConcurrentTimings(2, phase_gate=True)
    capture_started, release, attempted, scored = Event(), Event(), Event(), Event()
    real_lock = Lock()

    class ObservedLock:
        def __enter__(self):
            if capture_started.is_set():
                attempted.set()
            real_lock.acquire()

        def __exit__(self, *args):
            real_lock.release()

    timing._phase_gate_lock = ObservedLock()

    def capture():
        capture_started.set()
        assert release.wait(5)

    async def run():
        queue = R06CPUQueue()
        token = timing.active.set(dict(sample=0, started=perf_counter(), stages=[]))
        first = asyncio.create_task(asyncio.to_thread(timing.sync("actual_catalog_read_and_capture", capture)))
        timing.active.reset(token)
        try:
            assert await asyncio.to_thread(capture_started.wait, 5)

            def worker():
                token = timing.active.set(dict(sample=1, started=perf_counter(), stages=[]))
                try:
                    timing.sync("retrieval_and_ranking", scored.set)()
                finally:
                    timing.active.reset(token)

            task = asyncio.create_task(queue.run(worker))
            assert await asyncio.to_thread(attempted.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not scored.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert scored.is_set() and queue.outstanding == 0
            assert await queue.run(lambda: timing.current) is None
        finally:
            release.set()
            await first
            await queue.aclose()

    asyncio.run(run())
