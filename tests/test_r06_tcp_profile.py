"""Concurrent diagnostic attribution, behavior preservation and real TCP wiring."""

import asyncio
import gc
import json
import math
from contextvars import ContextVar
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import httpx
import pytest

from scripts import profile_r06_tcp as profiler
from scripts import r06_process_trace
from scripts.r06_process_trace import ConcurrentTimings, query_kind, trace_api
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


def test_staggered_asgi_requests_share_timeline_origin_and_report_by_sample(monkeypatch):
    clock = ContextVar("diagnostic_test_clock", default=100.0)
    monkeypatch.setattr(r06_process_trace, "perf_counter", lambda: clock.get())
    timing = ConcurrentTimings(2)

    async def run():
        first_entered, release_first = asyncio.Event(), asyncio.Event()

        async def app(request_scope, receive, send):
            index = int(request_scope["headers"][0][1])
            if index == 0:
                first_entered.set()
                await release_first.wait()
                clock.set(110.0)
            else:
                await first_entered.wait()
                clock.set(106.0)
            await send(dict(type="http.response.start", status=200))

        async def send(message):
            pass

        traced = timing.wrap(app)

        async def invoke(index, now):
            token = clock.set(now)
            try:
                await traced(scope(index), None, send)
            finally:
                clock.reset(token)

        first = asyncio.create_task(invoke(0, 101.25))
        await first_entered.wait()
        second = asyncio.create_task(invoke(1, 104.5))
        await second
        assert not first.done()  # The first and second request overlapped; second completed first.
        release_first.set()
        await first

    asyncio.run(run())
    assert [request["sample"] for request in timing.requests] == [1, 0]
    report = timing.report()
    assert [request["sample"] for request in report] == [0, 1]
    assert [request["asgi_start_offset_seconds"] for request in report] == [1.25, 4.5]
    assert [request["wall_seconds"] for request in report] == [8.75, 1.5]
    intervals = [(request["asgi_start_offset_seconds"],
                  request["asgi_start_offset_seconds"] + request["wall_seconds"])
                 for request in report]
    assert intervals == [(1.25, 10.0), (4.5, 6.0)]
    assert min(intervals[0][1], intervals[1][1]) - max(intervals[0][0], intervals[1][0]) == 1.5
    assert all("started" not in request for request in report)


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


@pytest.mark.parametrize("gc_events", [True, False])
def test_gc_event_observer_is_optional_and_restored(gc_events):
    timing = ConcurrentTimings(1)
    callbacks, enabled, threshold = list(gc.callbacks), gc.isenabled(), gc.get_threshold()
    with trace_api(timing, gc_events=gc_events):
        added = [callback for callback in gc.callbacks if callback not in callbacks]
        if gc_events:
            assert len(added) == 1 and added[0] == timing.gc
        else:
            assert gc.callbacks == callbacks
        assert gc.isenabled() == enabled and gc.get_threshold() == threshold
    assert gc.callbacks == callbacks and gc.isenabled() == enabled and gc.get_threshold() == threshold


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


def test_lab_gc_events_flag_requires_bool_before_validation(monkeypatch):
    from scripts import r06_service_lab as lab
    monkeypatch.setattr(lab, "validate", lambda *args: pytest.fail("reject flag first"))
    with pytest.raises(ValueError, match="gc_events"):
        R06ServiceLab(None, "PRIVATE", None, None, None, gc_events=1)


@pytest.mark.parametrize("profile_samples,gc_events,ready_gc_events,should_raise", [
    (2, False, False, False), (2, False, True, True), (0, True, False, False)])
def test_lab_propagates_gc_events_and_checks_ready(
        tmp_path, monkeypatch, profile_samples, gc_events, ready_gc_events, should_raise):
    from scripts import r06_service_lab as lab_module
    bundle_id = uuid4()
    monkeypatch.setattr(lab_module, "validate", lambda output, database_url, root, *_: (output, root, {}))
    monkeypatch.setattr(lab_module, "make_conninfo", lambda **kwargs: "PRIVATE-DB")
    lab = R06ServiceLab(tmp_path / "diagnostic", "PRIVATE", tmp_path, bundle_id, "b" * 64,
                        "stdlib", profile_samples=profile_samples, gc_events=gc_events)
    launched = {}

    class Process:
        def __init__(self, command, **kwargs):
            launched["command"] = command
            child_output = Path(command[3])
            child_output.mkdir(parents=True)
            ready = dict(run_id=lab.run_id, schema=lab.schema, bundle_id=str(bundle_id),
                         manifest_sha256=lab.digest, admin_enabled=False, pid=17, backend=lab.backend,
                         api_deadline_seconds=2.0, profile_samples=lab.profile_samples,
                         gc_events_enabled=ready_gc_events,
                         url="http://127.0.0.1:43210")
            (child_output / "ready.json").write_text(json.dumps(ready))

        def poll(self):
            return None

    monkeypatch.setattr(lab_module.subprocess, "Popen", Process)
    if should_raise:
        with pytest.raises(ValueError, match="identity changed"):
            lab.start()
    else:
        lab.start()
    assert ("--no-gc-events" in launched["command"]) is (not gc_events)
    assert lab.ready["gc_events_enabled"] is ready_gc_events


@pytest.mark.parametrize("source_changes", [False, True])
def test_profile_all_failures_remain_diagnostic_not_acceptance_and_source_bound(tmp_path, monkeypatch, source_changes):
    from scripts.run_r06_demo import marker
    monkeypatch.setattr(profiler, "__file__", str(tmp_path/"scripts"/"profile_r06_tcp.py"))
    commits = iter(("a"*40, ("b" if source_changes else "a")*40))
    monkeypatch.setattr(profiler, "_source", lambda _: next(commits))
    monkeypatch.setattr(profiler, "subprocess_sources", lambda _: {})
    class Lab:
        def __init__(self, output, *args, profile_samples, gc_events):
            assert profile_samples == 4
            assert gc_events is True
            self.output, self.child_output, self.created = output, output/"child", True
            self.ready = dict(url="http://127.0.0.1:1", model_version="approved", item_count=6,
                              gc_events_enabled=gc_events)
        def __enter__(self):
            self.child_output.mkdir(parents=True)
            marker(self.child_output, "profile", dict(requests=[dict(sample=i,status_code=504,
                                                                      asgi_start_offset_seconds=i * .25)
                                                                 for i in range(4)],
                                                        gc_events_enabled=True))
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
        assert report["shared_server_timeline_complete"] is True
        assert report["load"]["successful"] == 0 and report["load"]["failures"] == 2
        assert report["load"]["successful_latency"]["p95_ms"] is None
        assert report["gc_events_enabled"] is True
        assert report["successful_identities_valid"] is None
        assert report["successful_database_trace_count"] == 0
        assert report["successful_database_traces_complete"] is None
        assert not report["production_acceptance"] and not report["sla_proven"]
    assert "PRIVATE" not in (output/"observations.json").read_text()


@pytest.mark.parametrize("bad_offset", ["missing", "nan", "negative", "bool"])
def test_profile_marks_incomplete_shared_server_timeline(tmp_path, monkeypatch, bad_offset):
    monkeypatch.setattr(profiler, "__file__", str(tmp_path / "scripts" / "profile_r06_tcp.py"))
    monkeypatch.setattr(profiler, "_source", lambda _: "a" * 40)
    monkeypatch.setattr(profiler, "subprocess_sources", lambda _: {})

    offsets = [0.25, 0.5, 0.75, 1.0]
    requests = [dict(sample=i, status_code=504, asgi_start_offset_seconds=value)
                for i, value in enumerate(offsets)]
    if bad_offset == "missing":
        requests[1].pop("asgi_start_offset_seconds")
    elif bad_offset == "nan":
        requests[1]["asgi_start_offset_seconds"] = float("nan")
    elif bad_offset == "negative":
        requests[1]["asgi_start_offset_seconds"] = -0.01
    else:
        requests[1]["asgi_start_offset_seconds"] = True

    class Lab:
        def __init__(self, output, *args, profile_samples, gc_events):
            assert profile_samples == 4 and gc_events is True
            self.output, self.child_output, self.created = output, output / "child", True
            self.ready = dict(url="http://127.0.0.1:1", model_version="approved", item_count=6,
                              gc_events_enabled=True)

        def __enter__(self):
            self.child_output.mkdir(parents=True)
            (self.child_output / "profile.json").write_text(
                json.dumps(dict(requests=requests, gc_events_enabled=True), allow_nan=True),
                encoding="utf-8")
            return self

        def __exit__(self, *_):
            self.created = False

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def post(self, *args, **kwargs): return httpx.Response(201, json={})

    monkeypatch.setattr(profiler, "R06ServiceLab", Lab)
    monkeypatch.setattr(profiler.httpx, "Client", Client)
    monkeypatch.setattr(profiler, "sample", lambda url, ready, session, index, phase:
                        dict(sample=index, phase=phase, status_code=504, elapsed_ms=2000))
    report = profiler.profile(tmp_path / "artifacts" / "profile", "PRIVATE-DB", tmp_path,
                              uuid4(), "b" * 64, samples=2)
    assert report["trace_complete"]
    assert report["shared_server_timeline_complete"] is False


@pytest.mark.parametrize("gc_events", [True, False])
def test_real_synthetic_package_tcp_trace_is_correlated_and_drained(isolated_database, tmp_path, gc_events):
    root, target, digest = _build(tmp_path)
    output = Path(__file__).resolve().parents[1]/"artifacts"/"test-tcp-profile"/uuid4().hex
    lab = R06ServiceLab(output, isolated_database, root, UUID(target.name), digest, "stdlib",
                        profile_samples=2, gc_events=gc_events)
    with lab:
        assert lab.ready["profile_samples"] == 2
        assert lab.ready["gc_events_enabled"] is gc_events
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
    offsets = [request["asgi_start_offset_seconds"] for request in report["requests"]]
    assert all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in offsets)
    assert offsets[0] < offsets[1]
    for request in report["requests"]:
        assert {s["stage"] for s in request["stages"]} >= {
            "actual_catalog_read_and_capture", "cpu_queue_wait", "retrieval_and_ranking", "result_write",
            "publication_recovery_database_connect", "database_admission_database_connect",
            "database_admission_database_execute_session_snapshot_lock",
            "actual_catalog_read_and_capture_database_execute_actual_catalog_rows",
            "actual_catalog_read_and_capture_database_fetchall_decode",
            "result_write_database_commit", "result_write_database_close"}
    assert report["database_driver_version"]
    assert report["gc_events_enabled"] is gc_events
    if not gc_events:
        assert all(not stage["stage"].startswith("gc_generation_")
                   for request in report["requests"] for stage in request["stages"])
    assert not lab.created and lab.stopped[-1]["normal_cpu_drain"]
    assert not report["production_acceptance"] and session["access_token"] not in json.dumps(report)


@pytest.mark.parametrize("query,expected", [
    ("SELECT *\n FROM sessions WHERE session_id = %s FOR SHARE", "session_snapshot_lock"),
    ("SELECT * FROM recommendation_requests WHERE request_id = %s FOR UPDATE", "request_row_lock"),
    ("SELECT * FROM catalog_control WHERE singleton = 1 AND admission_open FOR SHARE", "catalog_barrier"),
    ("SELECT * FROM bundle_items bi JOIN items i ON i.item_id=bi.item_id", "actual_catalog_rows"),
    ("INSERT INTO recommendation_requests VALUES (%s)", "accepted_insert"),
    ("SELECT pg_advisory_lock(123)", "publication_lock"),
    ("SELECT pg_try_advisory_lock(%s)", "execution_lock"),
    (b"PRIVATE-QUERY", "other"),
    ("PRIVATE-QUERY", "other"),
    ("SELECT pg_advisory_lock(" + "x" * 4096, "other"),
])
def test_database_query_classifier_returns_only_fixed_labels(query, expected):
    assert query_kind(query) == expected


def test_database_classifier_never_stringifies_arbitrary_query():
    class Query:
        def __str__(self): pytest.fail("do not stringify SQL objects or private values")
    assert query_kind(Query()) == "other"


def test_database_timer_retains_results_errors_phase_and_bypasses_untraced_calls():
    timing = ConcurrentTimings(1)
    result, failure = object(), RuntimeError("PRIVATE-ERROR")
    calls = []
    def execute(cursor, query, parameters, **kwargs):
        calls.append((cursor, query, parameters, kwargs))
        if kwargs.get("fail"): raise failure
        return result
    timed = timing.database("execute", execute)
    # No request: exact pass-through, no SQL classification or retained record.
    assert timed(None, "PRIVATE-QUERY", ("PRIVATE-TOKEN",)) is result
    assert not timing.report()
    request = dict(sample=0, started=profiler.perf_counter(), stages=[])
    token = timing.active.set(request)
    try:
        def outer():
            assert timed(None, "SELECT * FROM sessions WHERE session_id = %s FOR SHARE",
                         ("PRIVATE-TOKEN",), binary=True) is result
            assert timing.phase.get() == "database_admission"
            with pytest.raises(RuntimeError) as caught:
                timed(None, "PRIVATE-QUERY", ("PRIVATE-TOKEN",), fail=True)
            assert caught.value is failure
        timing.sync("database_admission", outer)()
        assert timing.phase.get() == "other"
    finally:
        timing.active.reset(token)
    assert calls[1][-1] == {"binary": True}
    assert {s["stage"] for s in request["stages"]} == {
        "database_admission", "database_admission_database_execute_session_snapshot_lock",
        "database_admission_database_execute_other"}
    assert "PRIVATE" not in json.dumps(request["stages"])


@pytest.mark.parametrize("fail", [False, True])
def test_real_database_trace_preserves_commit_rollback_close_and_restores_patches(isolated_database, fail):
    import psycopg
    from psycopg.rows import namedtuple_row
    originals = (psycopg.connect, psycopg.Cursor.execute, psycopg.Cursor.fetchall,
                 psycopg.Connection.__exit__, psycopg.Connection.commit, psycopg.Connection.rollback)
    timing = ConcurrentTimings(1)
    failure = RuntimeError("PRIVATE-ERROR")
    rows = []
    async def app(*args):
        def work():
            with psycopg.connect(isolated_database) as connection:
                with connection.cursor(row_factory=namedtuple_row, binary=True) as cursor:
                    cursor.execute("SELECT 17::integer AS count, '中文'::text AS title")
                    rows.extend(cursor.fetchall())
                connection.execute("CREATE TABLE traced_commit (value integer)")
                connection.execute("INSERT INTO traced_commit VALUES (17)")
                if fail: raise failure
        return await asyncio.to_thread(timing.sync("database_admission", work))
    with trace_api(timing):
        if fail:
            with pytest.raises(RuntimeError) as caught:
                asyncio.run(timing.wrap(app)(scope(), None, None))
            assert caught.value is failure
        else:
            asyncio.run(timing.wrap(app)(scope(), None, None))
    assert originals == (psycopg.connect, psycopg.Cursor.execute, psycopg.Cursor.fetchall,
                         psycopg.Connection.__exit__, psycopg.Connection.commit, psycopg.Connection.rollback)
    assert rows[0].count == 17 and rows[0].title == "中文"
    with psycopg.connect(isolated_database) as connection:
        exists = connection.execute("SELECT to_regclass('traced_commit')").fetchone()[0]
        if fail: assert exists is None
        else: assert connection.execute("SELECT value FROM traced_commit").fetchall() == [(17,)]
    stages = {s["stage"] for s in timing.report()[0]["stages"]}
    assert stages >= {"database_admission_database_connect", "database_admission_database_execute_other",
                      "database_admission_database_fetchall_decode", "database_admission_database_cursor_close",
                      "database_admission_database_transaction_exit", "database_admission_database_close"}
    assert "database_admission_database_" + ("rollback" if fail else "commit") in stages
    assert "PRIVATE" not in json.dumps(timing.report())


def test_concurrent_database_phases_are_request_local_and_clear_after_errors():
    timing = ConcurrentTimings(2)
    arrived, release = Event(), Event()
    async def app(request_scope, *args):
        index = int(request_scope["headers"][0][1])
        phase = "database_admission" if index == 0 else "result_write"
        def work():
            if index == 0:
                arrived.set()
                assert release.wait(5)
            else:
                assert arrived.wait(5)
                release.set()
            assert timing.phase.get() == phase
            def execute(*args):
                if index == 0: raise ValueError("PRIVATE")
                return index
            return timing.database("execute", execute)(None, "PRIVATE-SQL", ("PRIVATE-PARAM",))
        try:
            return await asyncio.to_thread(timing.sync(phase, work))
        finally:
            assert timing.phase.get() == "other"
    async def run():
        results = await asyncio.gather(*(timing.wrap(app)(scope(i), None, None) for i in range(2)),
                                       return_exceptions=True)
        assert isinstance(results[0], ValueError) and results[1] == 1
        assert timing.current is None and timing.phase.get() == "other"
    asyncio.run(run())
    for index, request in enumerate(timing.report()):
        phase = "database_admission" if index == 0 else "result_write"
        assert {s["stage"] for s in request["stages"]} == {phase, phase + "_database_execute_other"}
    assert "PRIVATE" not in json.dumps(timing.report())


@pytest.mark.parametrize("count,coverage,identity,coverage_present,identity_present,expected", [
    (0, None, None, True, True, 0),
    (1, True, True, True, True, 0),
    (1, False, True, True, True, 1),
    (0, True, None, True, True, 1),
    (0, None, True, True, True, 1),
    (0, None, None, False, True, 1),
    (0, None, None, True, False, 1),
    (None, None, None, True, True, 1),
    (-1, None, None, True, True, 1),
    (True, True, True, True, True, 1),
    (1.0, True, True, True, True, 1),
    ("1", True, True, True, True, 1),
    (0, False, None, True, True, 1),
    (0, None, False, True, True, 1),
    (1, True, None, True, True, 1),
    (1, True, True, True, False, 1),
])
def test_cli_requires_consistent_successful_trace_summaries(
        monkeypatch, tmp_path, count, coverage, identity, coverage_present, identity_present, expected):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE")
    result_data = dict(status="instrumented_diagnostic_completed_not_performance_acceptance", trace_complete=True,
                       client_status_matches_server=True, successful_database_trace_count=count,
                       shared_server_timeline_complete=True)
    if coverage_present:
        result_data["successful_database_traces_complete"] = coverage
    if identity_present:
        result_data["successful_identities_valid"] = identity
    monkeypatch.setattr(profiler, "profile", lambda *args, **kwargs: result_data)
    result = profiler.main([str(tmp_path), str(tmp_path), str(uuid4()), "--expected-manifest-sha256", "b" * 64])
    assert result == expected


@pytest.mark.parametrize("timeline_present,timeline,expected", [
    (True, True, 0), (True, False, 1), (False, None, 1),
])
def test_cli_requires_shared_server_timeline_complete(
        monkeypatch, tmp_path, timeline_present, timeline, expected):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE")
    result_data = dict(status="instrumented_diagnostic_completed_not_performance_acceptance",
                       trace_complete=True, client_status_matches_server=True,
                       successful_database_trace_count=0,
                       successful_database_traces_complete=None, successful_identities_valid=None)
    if timeline_present:
        result_data["shared_server_timeline_complete"] = timeline
    monkeypatch.setattr(profiler, "profile", lambda *args, **kwargs: result_data)
    result = profiler.main([str(tmp_path), str(tmp_path), str(uuid4()),
                            "--expected-manifest-sha256", "b" * 64])
    assert result == expected


def test_cli_defaults_to_gc_events_and_accepts_no_gc_events_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE")
    seen = []

    def fake_profile(*args, **kwargs):
        seen.append(kwargs["gc_events"])
        return dict(status="instrumented_diagnostic_completed_not_performance_acceptance", trace_complete=True,
                    client_status_matches_server=True, gc_events_enabled=kwargs["gc_events"],
                    shared_server_timeline_complete=True,
                    successful_identities_valid=None, successful_database_trace_count=0,
                    successful_database_traces_complete=None)

    monkeypatch.setattr(profiler, "profile", fake_profile)
    args = [str(tmp_path), str(tmp_path), str(uuid4()), "--expected-manifest-sha256", "b" * 64]
    assert profiler.main(args) == 0
    assert profiler.main([*args, "--no-gc-events"]) == 0
    assert seen == [True, False]
