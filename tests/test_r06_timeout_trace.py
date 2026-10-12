"""Real timeout/drain lifetime and negative coverage controls, not load/SLA."""

import asyncio
from copy import deepcopy
import json
from threading import Event
from uuid import uuid4

import httpx
import pytest

from evorec.api.app import create_app
from evorec.application.recommend import Recommend
from evorec.infrastructure import postgres, r06_async
from evorec.infrastructure.management import CatalogManager
from evorec.infrastructure.postgres import PostgresDemoBackend
from evorec.infrastructure.r06_async import R06CPUQueue
from evorec.infrastructure.r06_serving import R06SnapshotRanker
from scripts import profile_r06_tcp as profiler
from scripts.r06_process_trace import ConcurrentTimings, trace_api
from test_r06_online import online, _command
from test_r06_tcp_profile import scope


def event(label, start, wall=0.01, error=None):
    return dict(stage=label, start_seconds=start, wall_seconds=wall,
                thread_cpu_seconds=None, error_type=error)


def complete_timeout(*, recovery=False):
    stages = [event("recommendation_workflow", 0, 3, "TimeoutError"),
              event("deadline_callback", 2), event("database_owned_work_drain", 2.1, .4),
              event("response_start_send", 3.1), event("response_body_complete_send", 3.2)]
    if recovery:
        stages.append(event("publication_recovery", .1, 2.3))
    else:
        stages.extend([event("execution_lease_and_admission", .1, 2.3),
                       event("execution_lease_and_admission_database_connect", .11),
                       event("execution_lease_close", 2.6, .2),
                       event("execution_lease_close_database_close", 2.61, .1)])
    return dict(status_code=504, error_type=None, wall_seconds=3.3, stages=stages)


@pytest.mark.parametrize("recovery", [True, False])
def test_timeout_coverage_accepts_recovery_without_fictitious_lease_or_owned_close(recovery):
    assert profiler._timeout_trace_complete(complete_timeout(recovery=recovery))


@pytest.mark.parametrize("label", [
    "recommendation_workflow", "deadline_callback", "database_owned_work_drain",
    "response_start_send", "response_body_complete_send", "execution_lease_close",
    "execution_lease_close_database_close",
])
@pytest.mark.parametrize("defect", ["missing", "error", "missing_error", "negative", "nan", "infinity", "bool"])
def test_timeout_coverage_rejects_each_missing_or_invalid_boundary(label, defect):
    request = complete_timeout()
    target = next(e for e in request["stages"] if e["stage"] == label)
    if defect == "missing":
        request["stages"].remove(target)
    elif defect == "error":
        target["error_type"] = "UnexpectedFailure"
    elif defect == "missing_error":
        target.pop("error_type")
    else:
        target["wall_seconds"] = {"negative": -1, "nan": float("nan"),
                                  "infinity": float("inf"), "bool": True}[defect]
    assert not profiler._timeout_trace_complete(request)


@pytest.mark.parametrize("defect", [
    "response_before_exit", "drain_before_callback", "drain_after_exit", "lease_before_work",
    "cpu_after_close", "write_after_close", "duplicate_callback", "outside_asgi", "no_recovery",
])
def test_timeout_coverage_rejects_inverted_ownership_or_ambiguous_events(defect):
    request = complete_timeout(recovery=defect == "no_recovery")
    by_label = {e["stage"]: e for e in request["stages"]}
    if defect == "response_before_exit": by_label["response_start_send"]["start_seconds"] = 2.9
    elif defect == "drain_before_callback": by_label["database_owned_work_drain"]["start_seconds"] = 1.5
    elif defect == "drain_after_exit": by_label["database_owned_work_drain"]["wall_seconds"] = 1.1
    elif defect == "lease_before_work": by_label["execution_lease_close"]["start_seconds"] = 2.2
    elif defect == "cpu_after_close": request["stages"].append(event("cpu_work", 2.7))
    elif defect == "write_after_close": request["stages"].append(event("result_write", 2.7))
    elif defect == "duplicate_callback": request["stages"].append(deepcopy(by_label["deadline_callback"]))
    elif defect == "outside_asgi": by_label["response_body_complete_send"]["wall_seconds"] = .2
    elif defect == "no_recovery": request["stages"].remove(by_label["publication_recovery"])
    assert not profiler._timeout_trace_complete(request)


@pytest.mark.parametrize("count,coverage,present,expected", [
    (0, None, True, 0), (1, True, True, 0), (1, False, True, 1), (0, True, True, 1),
    (0, None, False, 1), (None, None, True, 1), (-1, None, True, 1),
    (True, True, True, 1), (1.0, True, True, 1), ("1", True, True, 1), (0, False, True, 1),
])
def test_cli_requires_nonvacuous_timeout_coverage(monkeypatch, tmp_path, count, coverage, present, expected):
    result = dict(status="instrumented_diagnostic_completed_not_performance_acceptance",
                  trace_complete=True, client_status_matches_server=True, shared_server_timeline_complete=True,
                  successful_database_trace_count=0, successful_database_traces_complete=None,
                  successful_identities_valid=None, timeout_trace_count=count,
                  server_requests=[complete_timeout()] if count == 1 else [])
    if present: result["timeout_traces_complete"] = coverage
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE")
    monkeypatch.setattr(profiler, "profile", lambda *a, **kw: result)
    assert profiler.main([str(tmp_path), str(tmp_path), str(uuid4()),
                          "--expected-manifest-sha256", "b" * 64]) == expected


@pytest.mark.parametrize("defect", ["hidden_timeout", "forged_coverage", "missing_rows", "invalid_row"])
def test_cli_rechecks_actual_timeout_rows_instead_of_trusting_summary(monkeypatch, tmp_path, defect):
    request = complete_timeout()
    result = dict(status="instrumented_diagnostic_completed_not_performance_acceptance",
                  trace_complete=True, client_status_matches_server=True, shared_server_timeline_complete=True,
                  successful_database_trace_count=0, successful_database_traces_complete=None,
                  successful_identities_valid=None, timeout_trace_count=1, timeout_traces_complete=True,
                  server_requests=[request])
    if defect == "hidden_timeout": result.update(timeout_trace_count=0, timeout_traces_complete=None)
    elif defect == "forged_coverage": request["stages"] = []
    elif defect == "missing_rows": result.pop("server_requests")
    else: result["server_requests"] = [None]
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE")
    monkeypatch.setattr(profiler, "profile", lambda *a, **kw: result)
    assert profiler.main([str(tmp_path), str(tmp_path), str(uuid4()),
                          "--expected-manifest-sha256", "b" * 64]) == 1


def test_timeout_hooks_restore_and_do_not_log_exception_messages():
    originals = (asyncio.Timeout._on_timeout, Recommend.execute, postgres._drain, r06_async._drain)
    timing = ConcurrentTimings(1)
    failure = RuntimeError("PRIVATE-FAILURE")
    with pytest.raises(RuntimeError) as caught:
        with trace_api(timing, gc_events=False):
            assert originals != (asyncio.Timeout._on_timeout, Recommend.execute, postgres._drain, r06_async._drain)
            raise failure
    assert caught.value is failure
    assert originals == (asyncio.Timeout._on_timeout, Recommend.execute, postgres._drain, r06_async._drain)
    assert timing.report() == []


def test_absent_timeout_hook_fails_and_restores_already_installed_patches(monkeypatch):
    original = PostgresDemoBackend._admit
    monkeypatch.delattr(asyncio.Timeout, "_on_timeout")
    with pytest.raises(AttributeError):
        with trace_api(ConcurrentTimings(1), gc_events=False):
            pytest.fail("missing deadline observation was silently accepted")
    assert PostgresDemoBackend._admit is original


def test_concurrent_deadline_callbacks_and_sends_are_request_local():
    timing = ConcurrentTimings(2)
    async def app(request_scope, receive, send):
        index = int(request_scope["headers"][0][1])
        try:
            async with asyncio.timeout(.02 + index * .02):
                await asyncio.sleep(10)
        except TimeoutError:
            assert timing.current["sample"] == index
            await send(dict(type="http.response.start", status=504))
            await send(dict(type="http.response.body", body=b"PRIVATE", more_body=False))
    async def run():
        async def send(message): pass
        with trace_api(timing, gc_events=False):
            await asyncio.gather(*(timing.wrap(app)(scope(i), None, send) for i in range(2)))
        assert timing.current is None
    asyncio.run(run())
    for index, request in enumerate(timing.report()):
        assert request["sample"] == index and request["status_code"] == 504
        assert [e["stage"] for e in request["stages"]] == [
            "deadline_callback", "response_start_send", "response_body_complete_send"]
        assert not profiler._timeout_trace_complete(request)  # No real workflow/drain here.
    assert "PRIVATE" not in json.dumps(timing.report())


def test_repeated_cpu_cancellation_keeps_work_alive_and_records_finished_drain():
    timing, entered, release = ConcurrentTimings(1), Event(), Event()

    async def run():
        queue = R06CPUQueue()
        def work():
            entered.set()
            assert release.wait(5)
            return object()
        async def app(*args): return await queue.run(work)
        with trace_api(timing, gc_events=False):
            task = asyncio.create_task(timing.wrap(app)(scope(), None, None))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(.01)
                    assert not task.done() and timing.report() == [] and queue.outstanding == 1
                release.set()
                with pytest.raises(asyncio.CancelledError): await task
                assert queue.outstanding == 0
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await queue.aclose()
        assert timing.current is None
    asyncio.run(run())
    request, = timing.report()
    stages = {e["stage"]: e for e in request["stages"]}
    cpu, drain = stages["cpu_work"], stages["cpu_owned_work_drain"]
    assert cpu["start_seconds"] + cpu["wall_seconds"] <= drain["start_seconds"] + drain["wall_seconds"]
    assert stages["queue_and_cpu_drain"]["error_type"] == "CancelledError"
    assert drain["error_type"] is None and "deadline_callback" not in stages


@pytest.mark.parametrize("phase", ["recovery", "admission", "cpu", "save"])
def test_real_api_deadline_drains_owned_work_and_preserves_late_committed_result(online, monkeypatch, phase):
    application, _, _ = online
    targets = {"recovery": (CatalogManager, "ensure_ready"),
               "admission": (PostgresDemoBackend, "_admit"),
               "cpu": (R06SnapshotRanker, "score"), "save": (PostgresDemoBackend, "_save")}
    target, method = targets[phase]
    original = getattr(target, method)
    entered, release = Event(), Event()
    def block(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(target, method, block)
    timing = ConcurrentTimings(1)
    deadline_fired = Event()
    original_event = timing._event
    def signal_deadline(request, label, *args):
        original_event(request, label, *args)
        if label == "deadline_callback": deadline_fired.set()
    monkeypatch.setattr(timing, "_event", signal_deadline)

    async def run():
        command = await _command(application)
        headers = {"X-Session-Token": command.session_token, "Idempotency-Key": str(command.request_id),
                   "X-EvoRec-Diagnostic-Sample": "0"}
        payload = dict(session_id=str(command.session_id), expected_history_version=0, strategy="dense", k=2)
        with trace_api(timing, gc_events=False):
            app = timing.wrap(create_app(demo_application=application))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                task = asyncio.create_task(client.post("/api/v1/recommendations", headers=headers, json=payload))
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    assert await asyncio.to_thread(deadline_fired.wait, 4)
                    assert not task.done() and timing.report() == []
                    assert application.backend.r06_queue.outstanding == (1 if phase == "cpu" else 0)
                    execution = application.backend._execution(command.request_id)
                    if phase == "recovery": assert execution is None
                    else:
                        assert execution is not None and not execution.connection.closed
                        execution.assert_held()
                    release.set()
                    response = await task
                    assert response.status_code == 504
                    assert response.json()["error"]["code"] == "recommendation_timeout"
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                assert application.backend.r06_queue.outstanding == 0
                assert not application.backend._executions
                with application.backend._connect() as connection:
                    row = connection.execute("SELECT status FROM recommendation_requests WHERE request_id=%s",
                                             (command.request_id,)).fetchone()
                    count = connection.execute("SELECT count(*) AS n FROM request_items WHERE request_id=%s",
                                               (command.request_id,)).fetchone()["n"]
                if phase == "recovery": assert row is None and count == 0
                elif phase == "save": assert row["status"] == "completed" and count == 2
                else: assert row["status"] == "failed" and count == 0
                if phase == "save":
                    replay = await client.post("/api/v1/recommendations", headers=headers, json=payload)
                    assert replay.status_code == 200 and len(replay.json()["items"]) == 2
        assert timing.current is None

    asyncio.run(run())
    request, = timing.report()
    assert profiler._timeout_trace_complete(request)
    assert "PRIVATE" not in json.dumps(request)
