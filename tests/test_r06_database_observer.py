"""Real wait/cancel controls plus fail-closed, bounded and secret-free reports."""

from datetime import datetime, timezone
import os
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace

import psycopg
import pytest

from scripts import r06_database_observer as module
from scripts.profile_r06_catalog_capture import _execute_observed, main, profile
from scripts.r06_database_observer import DatabaseCallObserver, _sanitized_sample


def _sample(**changes):
    return dict(state="active", wait_event_type=None, wait_event=None, blocking_count=0, **changes)


@pytest.mark.parametrize("kwargs", [{"max_samples": value} for value in (0, 4097, True, 1.5)] +
                         [{"interval": value} for value in (0, .019, 1.01, True, float("nan"), float("inf"))])
def test_invalid_resource_bounds_reject_before_touching_connection(kwargs):
    with pytest.raises(ValueError):
        DatabaseCallObserver(None, "never connect", **kwargs)


@pytest.mark.parametrize("closed,autocommit", [(True, True), (False, False)])
def test_live_autocommit_required_before_identity_sql(closed, autocommit):
    with pytest.raises(ValueError, match="live owned autocommit"):
        DatabaseCallObserver(SimpleNamespace(closed=closed, autocommit=autocommit), "unused")


def test_sample_drops_sql_credentials_and_blocker_identity():
    row = _sample(query="secret SQL", parameters="private", dsn="secret", blocker_pid=123)
    assert _sanitized_sample(row, .1) == dict(offset_seconds=.1, state="active",
        wait_event_type=None, wait_event=None, blocking_count=0)


@pytest.mark.parametrize("field,value", [("state", None), ("state", "disabled"),
    ("wait_event", "x"*81), ("wait_event_type", 1), ("blocking_count", True), ("blocking_count", -1)])
def test_invalid_or_unavailable_activity_is_not_success(field, value):
    row = _sample()
    row[field] = value
    with pytest.raises(ValueError):
        _sanitized_sample(row, .1)


@pytest.mark.parametrize("offset", [-1, True, float("nan"), float("inf")])
def test_sample_offsets_are_finite_nonnegative(offset):
    with pytest.raises(ValueError):
        _sanitized_sample(_sample(), offset)


def test_backend_disappearance_rejects():
    with pytest.raises(ValueError, match="identity disappeared"):
        _sanitized_sample(None, .1)


@pytest.mark.parametrize("image,age,valid", [("postgres.exe", .5, True), ("python.exe", .5, False),
                                         ("postgres.exe", -1, False), ("postgres.exe", 3, False)])
def test_native_cpu_requires_image_and_creation_identity(monkeypatch, image, age, valid):
    when = datetime(2026, 10, 10, tzinfo=timezone.utc)
    created = int((when.timestamp()-age+11_644_473_600)*10_000_000)
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "_windows_process_times", lambda pid: (created, 2.5, image))
    assert module._server_cpu(42, when) == ((created, 2.5) if valid else None)


def test_non_windows_pid_mapping_stays_unknown(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module, "_windows_process_times", lambda pid: pytest.fail("host PID must not be read"))
    assert module._server_cpu(42, datetime.now(timezone.utc)) is None


def test_cpu_access_failure_is_unknown(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "win32")
    def denied(pid):
        raise OSError("private diagnostic must not be exported")
    monkeypatch.setattr(module, "_windows_process_times", denied)
    assert module._server_cpu(42, datetime.now(timezone.utc)) is None


def test_native_handle_reads_own_process_without_platform_skip():
    if module.sys.platform == "win32":
        created, cpu, image = module._windows_process_times(os.getpid())
        assert created > 0 and cpu >= 0 and "python" in image
    else:
        assert module._server_cpu(os.getpid(), datetime.now(timezone.utc)) is None


def _assert_reader_gone(connection, report):
    assert report["observer_thread_joined"] and report["observer_pid"] != connection.info.backend_pid
    assert connection.execute("SELECT count(*) FROM pg_stat_activity WHERE pid=%s",
                              (report["observer_pid"],)).fetchone()[0] == 0
    assert connection.execute("SELECT 1").fetchone() == (1,)


def _wait_for(connection, pid, wait_type, wait_event=None):
    deadline = monotonic()+5
    while monotonic() < deadline:
        row = connection.execute("SELECT wait_event_type,wait_event FROM pg_stat_activity WHERE pid=%s",
                                 (pid,)).fetchone()
        if row and row[0] == wait_type and (wait_event is None or row[1] == wait_event):
            return
        sleep(.02)
    pytest.fail("owned backend did not reach expected wait")


def test_actual_sleep_is_not_mislabeled_lock_and_reader_drains(isolated_database):
    with psycopg.connect(isolated_database, autocommit=True) as target:
        observer = DatabaseCallObserver(target, isolated_database)
        with observer:
            target.execute("SELECT pg_sleep(.2)")
        report = observer.report()
        assert report["observer_complete"] and report["active_sample_count"] > 0
        assert any(row["wait_event_type"] == "Timeout" and row["wait_event"] == "PgSleep"
                   for row in report["samples"])
        assert report["blocking_sample_count"] == 0
        assert not any(row["wait_event_type"] == "Lock" for row in report["samples"])
        _assert_reader_gone(target, report)
        with pytest.raises(ValueError, match="cannot be reused"):
            observer.__enter__()


def _operation_thread(observer, target, query, errors):
    def run():
        try:
            with observer:
                target.execute(query)
        except Exception as error:
            errors.append(error)
    worker = Thread(target=run, name="owned-test-query")
    worker.start()
    return worker


def test_real_row_lock_has_sampled_blocker_not_execute_guess(isolated_database):
    with psycopg.connect(isolated_database, autocommit=True) as target, \
         psycopg.connect(isolated_database) as blocker, \
         psycopg.connect(isolated_database, autocommit=True) as control:
        target.execute("CREATE TABLE observer_probe(id integer PRIMARY KEY, value integer)")
        target.execute("INSERT INTO observer_probe VALUES (1,0)")
        blocker.execute("SELECT * FROM observer_probe WHERE id=1 FOR UPDATE")
        observer, errors = DatabaseCallObserver(target, isolated_database), []
        worker = _operation_thread(observer, target, "UPDATE observer_probe SET value=1 WHERE id=1", errors)
        try:
            _wait_for(control, target.info.backend_pid, "Lock")
            sleep(.15)  # Hold the known lock across multiple observer intervals.
        finally:
            blocker.rollback()
            worker.join(8)
        assert not worker.is_alive() and errors == []
        report = observer.report()
        assert report["observer_complete"] and report["blocking_sample_count"] > 0
        assert any(row["wait_event_type"] == "Lock" and row["blocking_count"] > 0
                   for row in report["samples"])
        _assert_reader_gone(target, report)


def test_cancelled_owned_sql_keeps_error_and_closes_reader(isolated_database):
    with psycopg.connect(isolated_database, autocommit=True) as target, \
         psycopg.connect(isolated_database, autocommit=True) as control:
        observer, errors = DatabaseCallObserver(target, isolated_database), []
        worker = _operation_thread(observer, target, "SELECT pg_sleep(5)", errors)
        try:
            _wait_for(control, target.info.backend_pid, "Timeout", "PgSleep")
            sleep(.1)
        finally:
            try:
                assert control.execute("SELECT pg_cancel_backend(%s)", (target.info.backend_pid,)).fetchone() == (True,)
            finally:
                worker.join(8)
        assert not worker.is_alive() and len(errors) == 1 and isinstance(errors[0], psycopg.errors.QueryCanceled)
        report = observer.report()
        assert report["operation_error_type"] == "QueryCanceled" and report["observer_complete"]
        _assert_reader_gone(target, report)


def test_sample_cap_and_no_active_coverage_fail_closed(isolated_database):
    with psycopg.connect(isolated_database, autocommit=True) as target:
        observer = DatabaseCallObserver(target, isolated_database, max_samples=1)
        with observer:
            target.execute("SELECT pg_sleep(.1)")
        report = observer.report()
        assert len(report["samples"]) == 1 and report["truncated"] and not report["observer_complete"]
        assert report["active_sample_count"] == 0
        _assert_reader_gone(target, report)


def test_reader_error_is_sanitized_not_a_success(isolated_database, monkeypatch):
    with psycopg.connect(isolated_database, autocommit=True) as target:
        observer = DatabaseCallObserver(target, isolated_database)
        def failed_connect(*args, **kwargs):
            raise psycopg.OperationalError("SECRET URL SQL password")
        with monkeypatch.context() as patch:
            patch.setattr(module.psycopg, "connect", failed_connect)
            with observer:
                target.execute("SELECT 1")
        report = observer.report()
        assert report["observer_error_type"] == "OperationalError" and not report["observer_complete"]
        assert report["samples"] == [] and "SECRET" not in str(report)
        assert observer.joined


@pytest.mark.parametrize("before,after,expected", [((1,2.), (1,2.25), .25),
    ((1,2.), (2,2.25), None), ((1,2.), (1,1.), None), (None, (1,2.), None)])
def test_cpu_delta_requires_same_birth_and_monotonic_value(isolated_database, monkeypatch, before, after, expected):
    values = iter((before, after))
    monkeypatch.setattr(module, "_server_cpu", lambda *args: next(values))
    with psycopg.connect(isolated_database, autocommit=True) as target:
        observer = DatabaseCallObserver(target, isolated_database)
        with observer:
            target.execute("SELECT pg_sleep(.06)")
        assert observer.report()["server_leader_cpu_seconds"] == expected
        _assert_reader_gone(target, observer.report())


def test_optional_cpu_failure_does_not_mask_sql_or_leave_reader(isolated_database, monkeypatch):
    def failed_cpu(*args):
        raise RuntimeError("PRIVATE NATIVE FAILURE")
    monkeypatch.setattr(module, "_server_cpu", failed_cpu)
    with psycopg.connect(isolated_database, autocommit=True) as target:
        observer = DatabaseCallObserver(target, isolated_database)
        with pytest.raises(psycopg.errors.DivisionByZero):
            with observer:
                target.execute("SELECT 1/0")
        report = observer.report()
        assert report["server_leader_cpu_seconds"] is None and report["cpu_error_type"] == "RuntimeError"
        assert report["operation_error_type"] == "DivisionByZero" and "PRIVATE" not in str(report)
        _assert_reader_gone(target, report)


def test_profiler_default_off_does_not_construct_reader(isolated_database, monkeypatch):
    monkeypatch.setattr("scripts.profile_r06_catalog_capture.DatabaseCallObserver",
                        lambda *args: pytest.fail("default-off must not observe"))
    with psycopg.connect(isolated_database, autocommit=True) as target:
        observation = {}
        assert _execute_observed(target, "unused", "SELECT 1 AS value", (), many=False,
                                 enabled=False, observation=observation) == {"value": 1}
        assert observation["database_observation"] is None and observation["execute_ms"] >= 0


def test_profiler_keeps_failed_call_observation_without_retry(isolated_database):
    with psycopg.connect(isolated_database, autocommit=True) as target:
        observation = {}
        with pytest.raises(psycopg.errors.DivisionByZero):
            _execute_observed(target, isolated_database, "SELECT 1/0", (), many=False,
                              enabled=True, observation=observation)
        assert observation["operation_error_type"] == "DivisionByZero" and observation["execute_ms"] is None
        assert observation["database_observation"]["operation_error_type"] == "DivisionByZero"
        _assert_reader_gone(target, observation["database_observation"])


def test_invalid_observer_option_rejects_before_source_or_lab():
    with pytest.raises(ValueError, match="must be a bool"):
        profile(None, None, None, None, None, observe_waits="yes")


@pytest.mark.parametrize("count,complete,code", [(8,True,0), (8,False,1), (0,True,1), (7,True,1)])
def test_cli_does_not_pass_empty_or_incomplete_observations(monkeypatch, capsys, count, complete, code):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "not exported")
    monkeypatch.setattr("sys.argv", ["probe", "out", "root", "00000000-0000-0000-0000-000000000000",
                                    "--expected-manifest-sha256", "f"*64, "--observe-waits"])
    monkeypatch.setattr("scripts.profile_r06_catalog_capture.profile", lambda *args, **kwargs:
        dict(item_count=1, measurements=[dict(database_observation=dict(observer_complete=complete))]*count))
    assert main() == code
    output = capsys.readouterr().out
    assert ("incomplete_observations" in output) == (code == 1)
    assert "not exported" not in output
