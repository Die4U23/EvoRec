"""Concurrent diagnostic attribution, behavior preservation and real TCP wiring."""

import asyncio
import gc
import json
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import httpx
import pytest

from scripts import profile_r06_tcp as profiler
from scripts.r06_process_trace import ConcurrentTimings, trace_api
from scripts.r06_service_lab import R06ServiceLab
from evorec.infrastructure.r06_async import R06CPUQueue
from test_r06_bundle import _build


def scope(index=0, **changes):
    return dict(type="http", method="POST", path="/api/v1/recommendations",
                headers=[(b"x-evorec-diagnostic-sample", str(index).encode()),
                         (b"x-session-token", b"PRIVATE-TOKEN")]) | changes


@pytest.mark.parametrize("bad", [0, 123, True, "2"])
def test_trace_bounds_rejected(bad):
    with pytest.raises(ValueError): ConcurrentTimings(bad)


def test_concurrent_async_and_to_thread_records_do_not_cross_contaminate():
    timing = ConcurrentTimings(2)
    async def run():
        arrived, release = asyncio.Event(), asyncio.Event()
        async def app(request_scope, receive, send):
            index = int(request_scope["headers"][0][1])
            assert timing.current["sample"] == index
            if index == 0:
                arrived.set()
                await release.wait()
            else:
                await arrived.wait()
                release.set()
            def work():
                assert timing.current["sample"] == index
                return index
            assert await asyncio.to_thread(timing.sync(f"worker_{index}", work)) == index
            await send(dict(type="http.response.start", status=200+index))
        async def send(message): pass
        await asyncio.gather(*(timing.wrap(app)(scope(i), None, send) for i in range(2)))
        assert timing.current is None
    asyncio.run(run())
    for index, request in enumerate(timing.report()):
        assert request["sample"] == index and request["status_code"] == 200+index
        assert [s["stage"] for s in request["stages"]] == [f"worker_{index}"]
    assert "PRIVATE" not in json.dumps(timing.report())


def test_real_cpu_pool_inherits_trace_and_clears_reused_thread():
    timing = ConcurrentTimings(2)
    async def run():
        pool = R06CPUQueue()
        try:
            async def app(request_scope, receive, send):
                index = int(request_scope["headers"][0][1])
                def work():
                    assert timing.current["sample"] == index
                    return object()
                result = await timing.async_stage("queue_and_cpu_drain", R06CPUQueue.run)(
                    pool, timing.sync(f"cpu_{index}", work))
                assert result is not None
                await send(dict(type="http.response.start", status=200))
            async def send(message): pass
            await asyncio.gather(*(timing.wrap(app)(scope(i), None, send) for i in range(2)))
            assert await pool.run(lambda: timing.current) is None
        finally:
            await pool.aclose()
    asyncio.run(run())
    for index, request in enumerate(timing.report()):
        assert {s["stage"] for s in request["stages"]} == {
            "cpu_queue_wait", "queue_and_cpu_drain", f"cpu_{index}"}
        assert all(s["wall_seconds"] >= 0 for s in request["stages"])


def test_cancelled_request_trace_waits_for_real_cpu_drain():
    timing, started, release = ConcurrentTimings(1), Event(), Event()
    async def run():
        pool = R06CPUQueue()
        try:
            def work():
                started.set()
                assert release.wait(5)
                assert timing.current["sample"] == 0
            async def app(*args):
                return await timing.async_stage("queue_and_cpu_drain", R06CPUQueue.run)(
                    pool, timing.sync("real_cpu", work))
            task = asyncio.create_task(timing.wrap(app)(scope(), None, None))
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and timing.report() == []
            release.set()
            with pytest.raises(asyncio.CancelledError): await task
            assert pool.outstanding == 0
            assert await pool.run(lambda: timing.current) is None
        finally:
            release.set()
            await pool.aclose()
    asyncio.run(run())
    request, = timing.report()
    assert request["error_type"] == "asgi_exception"
    assert next(s for s in request["stages"] if s["stage"] == "queue_and_cpu_drain")["error_type"] == "CancelledError"


@pytest.mark.parametrize("changes", [dict(type="lifespan"), dict(method="GET"),
    dict(path="/api/v1/sessions"), dict(headers=[]),
    dict(headers=[(b"x-evorec-diagnostic-sample", b"PRIVATE")]),
    dict(headers=[(b"x-evorec-diagnostic-sample", b"000")]),
    dict(headers=[(b"x-evorec-diagnostic-sample", b"999")]),
    dict(headers=[(b"x-evorec-diagnostic-sample", b"0")]*2)])
def test_invalid_or_nonrecommendation_scope_passes_through_unobserved(changes):
    timing, result = ConcurrentTimings(1), object()
    async def app(*args): return result
    assert asyncio.run(timing.wrap(app)(scope(**changes), None, None)) is result
    assert timing.report() == []


def test_failure_identity_capacity_and_callbacks_preserved():
    timing, failure = ConcurrentTimings(1), RuntimeError("PRIVATE-ERROR")
    callbacks, enabled, threshold = list(gc.callbacks), gc.isenabled(), gc.get_threshold()
    async def app(*args): raise failure
    with pytest.raises(RuntimeError) as caught:
        with trace_api(timing):
            asyncio.run(timing.wrap(app)(scope(), None, None))
    assert caught.value is failure
    assert gc.callbacks == callbacks and gc.isenabled() == enabled and gc.get_threshold() == threshold
    assert "PRIVATE" not in json.dumps(timing.report())
    async def untouched(*args): return "unchanged"
    assert asyncio.run(timing.wrap(untouched)(scope(), None, None)) == "unchanged"
    assert len(timing.report()) == 1


@pytest.mark.parametrize("status", [200, 504])
def test_client_phase_timer_omits_secrets_and_has_no_retry(monkeypatch, status):
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        def post(self, *args, **kwargs):
            calls.append(kwargs)
            return httpx.Response(status, json={"secret": "PRIVATE-RESPONSE"})
        def close(self): pass
    monkeypatch.setattr(profiler.httpx, "Client", Client)
    monkeypatch.setattr(profiler, "legal", lambda *args: True)
    result = profiler.sample("http://127.0.0.1:1", {},
        dict(access_token="PRIVATE-TOKEN", session_id="PRIVATE-SESSION", history_version=0), 1, "load")
    assert len(calls) == 1 and calls[0]["headers"]["X-EvoRec-Diagnostic-Sample"] == "1"
    assert result["status_code"] == status and result["elapsed_ms"] >= 0
    assert all(result[key] >= 0 for key in ("client_create_ms", "exchange_ms", "close_ms"))
    assert result["response_identity_valid"] == (True if status == 200 else None)
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("bad", [True, -1, 123, "2"])
def test_lab_profile_flag_rejected_before_validation_or_database(bad, monkeypatch):
    from scripts import r06_service_lab as lab
    monkeypatch.setattr(lab, "validate", lambda *args: pytest.fail("reject flag first"))
    with pytest.raises(ValueError):
        R06ServiceLab(None, "PRIVATE", None, None, None, profile_samples=bad)


@pytest.mark.parametrize("source_changes", [False, True])
def test_profile_all_failures_remain_diagnostic_not_acceptance_and_source_bound(tmp_path, monkeypatch, source_changes):
    from scripts.run_r06_demo import marker
    monkeypatch.setattr(profiler, "__file__", str(tmp_path/"scripts"/"profile_r06_tcp.py"))
    commits = iter(("a"*40, ("b" if source_changes else "a")*40))
    monkeypatch.setattr(profiler, "_source", lambda _: next(commits))
    monkeypatch.setattr(profiler, "subprocess_sources", lambda _: {})
    class Lab:
        def __init__(self, output, *args, profile_samples):
            assert profile_samples == 4
            self.output, self.child_output, self.created = output, output/"child", True
            self.ready = dict(url="http://127.0.0.1:1", model_version="approved", item_count=6)
        def __enter__(self):
            self.child_output.mkdir(parents=True)
            marker(self.child_output, "profile", dict(requests=[dict(sample=i,status_code=504) for i in range(4)]))
            return self
        def __exit__(self, *_): self.created = False
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def post(self, *args, **kwargs): return httpx.Response(201,json={"private":"PRIVATE"})
    monkeypatch.setattr(profiler, "R06ServiceLab", Lab)
    monkeypatch.setattr(profiler.httpx, "Client", Client)
    monkeypatch.setattr(profiler, "sample", lambda url, ready, session, index, phase:
                        dict(sample=index,phase=phase,status_code=504,elapsed_ms=2000))
    output = tmp_path/"artifacts"/"profile"
    if source_changes:
        with pytest.raises(ValueError,match="source changed"):
            profiler.profile(output,"PRIVATE-DB",tmp_path,uuid4(),"b"*64,samples=2)
        assert not (output/"profile.json").exists()
    else:
        report = profiler.profile(output,"PRIVATE-DB",tmp_path,uuid4(),"b"*64,samples=2)
        assert report["trace_complete"] and report["client_status_matches_server"]
        assert report["load"]["successful"] == 0 and report["load"]["failures"] == 2
        assert report["load"]["successful_latency"]["p95_ms"] is None
        assert not report["production_acceptance"] and not report["sla_proven"]
    assert "PRIVATE" not in (output/"observations.json").read_text()


def test_real_synthetic_package_tcp_trace_is_correlated_and_drained(isolated_database, tmp_path):
    root, target, digest = _build(tmp_path)
    output = Path(__file__).resolve().parents[1]/"artifacts"/"test-tcp-profile"/uuid4().hex
    lab = R06ServiceLab(output, isolated_database, root, UUID(target.name), digest, "stdlib", profile_samples=2)
    with lab:
        assert lab.ready["profile_samples"] == 2
        with httpx.Client(base_url=lab.ready["url"], timeout=10, trust_env=False) as client:
            session = client.post("/api/v1/sessions", json={"profile_id":"sample"}).json()
            for index in range(2):
                response = client.post("/api/v1/recommendations", headers={
                    "X-Session-Token":session["access_token"], "Idempotency-Key":str(uuid4()),
                    "X-EvoRec-Diagnostic-Sample":str(index)}, json={
                    "session_id":session["session_id"], "expected_history_version":0,"strategy":"dense","k":2})
                assert response.status_code == 200
    report = json.loads((lab.child_output/"profile.json").read_bytes())
    assert [r["sample"] for r in report["requests"]] == [0,1]
    assert all(r["status_code"] == 200 for r in report["requests"])
    for request in report["requests"]:
        assert {s["stage"] for s in request["stages"]} >= {
            "actual_catalog_read_and_capture", "cpu_queue_wait", "retrieval_and_ranking", "result_write"}
    assert not lab.created and lab.stopped[-1]["normal_cpu_drain"]
    assert not report["production_acceptance"] and session["access_token"] not in json.dumps(report)
