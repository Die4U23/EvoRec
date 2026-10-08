"""Concurrent diagnostic attribution, behavior preservation and real TCP wiring."""

import asyncio
from contextlib import nullcontext
import gc
import json
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import httpx
import pytest

from scripts import profile_r06_tcp as profiler
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
        assert report["successful_database_trace_count"] == 0
        assert report["successful_database_traces_complete"] is None
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
            "actual_catalog_read_and_capture", "cpu_queue_wait", "retrieval_and_ranking", "result_write",
            "publication_recovery_database_connect", "database_admission_database_connect",
            "database_admission_database_execute_session_snapshot_lock",
            "actual_catalog_read_and_capture_database_execute_actual_catalog_rows",
            "actual_catalog_read_and_capture_database_fetchall_decode",
            "result_write_database_execute_request_row_lock",
            "result_write_database_transaction_block_exit", "execution_lease_close_database_close"}
        assert sum(s["stage"].endswith("_database_connect") for s in request["stages"]) == 3
        assert "result_write_database_connect" not in {s["stage"] for s in request["stages"]}
    assert report["database_driver_version"]
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
@pytest.mark.parametrize("transaction_block", [False, True])
def test_real_database_trace_preserves_commit_rollback_close_and_restores_patches(isolated_database, fail, transaction_block):
    import psycopg
    from psycopg.rows import namedtuple_row
    originals = (psycopg.connect, psycopg.Cursor.execute, psycopg.Cursor.fetchall,
                 psycopg.Connection.__exit__, psycopg.Connection.commit, psycopg.Connection.rollback,
                 psycopg.Transaction.__exit__)
    timing = ConcurrentTimings(1)
    failure = RuntimeError("PRIVATE-ERROR")
    rows = []
    async def app(*args):
        def work():
            with psycopg.connect(isolated_database, autocommit=transaction_block) as connection:
                with connection.transaction() if transaction_block else nullcontext():
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
                         psycopg.Connection.__exit__, psycopg.Connection.commit, psycopg.Connection.rollback,
                         psycopg.Transaction.__exit__)
    assert rows[0].count == 17 and rows[0].title == "中文"
    with psycopg.connect(isolated_database) as connection:
        exists = connection.execute("SELECT to_regclass('traced_commit')").fetchone()[0]
        if fail: assert exists is None
        else: assert connection.execute("SELECT value FROM traced_commit").fetchall() == [(17,)]
    stages = {s["stage"] for s in timing.report()[0]["stages"]}
    assert stages >= {"database_admission_database_connect", "database_admission_database_execute_other",
                      "database_admission_database_fetchall_decode", "database_admission_database_cursor_close",
                      "database_admission_database_transaction_exit", "database_admission_database_close"}
    if transaction_block:
        assert "database_admission_database_transaction_block_exit" in stages
    else:
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


@pytest.mark.parametrize("count,complete,expected", [
    (0, None, 0),       # All-failed, fully collected diagnostics are not acceptance.
    (2, True, 0),        # Successful requests have complete database traces.
    (2, False, 1),       # At least one successful request is missing required stages.
    (2, None, 1),        # Positive count with unknown completeness fails closed.
    (0, True, 1),        # Zero count cannot claim completeness.
    (None, None, 1),     # Older or malformed results missing the count fail closed.
])
def test_cli_requires_consistent_successful_database_trace_summary(monkeypatch, tmp_path, count, complete, expected):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE")
    result_data = dict(status="instrumented_diagnostic_completed_not_performance_acceptance", trace_complete=True,
                       client_status_matches_server=True, successful_identities_valid=True,
                       successful_database_trace_count=count,
                       successful_database_traces_complete=complete)
    monkeypatch.setattr(profiler, "profile", lambda *args, **kwargs: result_data)
    result = profiler.main([str(tmp_path), str(tmp_path), str(uuid4()), "--expected-manifest-sha256", "b" * 64])
    assert result == expected
