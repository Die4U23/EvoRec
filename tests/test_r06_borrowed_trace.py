"""Real PostgreSQL checks for borrowed-connection tracing and trace completeness."""

import asyncio
import json

import psycopg
import pytest

from scripts import profile_r06_tcp as profiler
from scripts.r06_process_trace import ConcurrentTimings, trace_api


REQUIRED_DATABASE_STAGES = {
    "publication_recovery_database_connect",
    "execution_lease_and_admission_database_connect",
    "execution_lease_and_admission_database_execute_execution_lock",
    "database_admission_database_execute_session_snapshot_lock",
    "database_admission_database_execute_accepted_insert",
    "database_admission_database_transaction_context_exit",
    "actual_catalog_read_and_capture_database_execute_actual_catalog_rows",
    "actual_catalog_read_and_capture_database_fetchall_decode",
    "result_write_database_connect",
    "result_write_database_commit",
    "result_write_database_close",
    "execution_lease_close_database_close",
}


def _complete_request():
    return {
        "error_type": None,
        "stages": [
            {"stage": stage, "error_type": None}
            for stage in sorted(REQUIRED_DATABASE_STAGES)
        ],
    }


@pytest.mark.parametrize("missing", sorted(REQUIRED_DATABASE_STAGES))
def test_database_trace_completeness_rejects_each_missing_required_stage(missing):
    request = _complete_request()
    request["stages"] = [event for event in request["stages"] if event["stage"] != missing]

    assert profiler._fresh_database_trace_complete(request) is False


def test_database_trace_completeness_requires_known_error_free_metadata():
    request = _complete_request()
    assert profiler._fresh_database_trace_complete(request) is True

    request["stages"][0]["error_type"] = "RaiseException"
    assert profiler._fresh_database_trace_complete(request) is False

    request = _complete_request()
    del request["stages"][0]["error_type"]
    assert profiler._fresh_database_trace_complete(request) is False

    request = _complete_request()
    request["error_type"] = "unrecognized-request-error-metadata"
    assert profiler._fresh_database_trace_complete(request) is False

    request = _complete_request()
    del request["error_type"]
    assert profiler._fresh_database_trace_complete(request) is False


@pytest.mark.parametrize("required,obsolete", [
    ("execution_lease_and_admission_database_connect", "database_admission_database_connect"),
    ("database_admission_database_transaction_context_exit", "database_admission_database_transaction_exit"),
])
def test_owned_connection_events_cannot_substitute_for_borrowed_boundaries(required, obsolete):
    request = _complete_request()
    for event in request["stages"]:
        if event["stage"] == required:
            event["stage"] = obsolete
    assert profiler._fresh_database_trace_complete(request) is False


@pytest.mark.parametrize("fail", [False, True])
def test_real_database_trace_records_borrowed_transaction_context_exit(
        isolated_database, fail):
    from psycopg.pq import TransactionStatus

    failure = RuntimeError("PRIVATE-FAILURE-MARKER")
    timing = ConcurrentTimings(1)
    original_transaction_exit = psycopg.Transaction.__exit__
    connection = psycopg.connect(isolated_database, autocommit=True)
    connection.execute("CREATE TABLE borrowed_trace_rows (value integer)")

    async def app(*_args):
        def work():
            with connection.transaction():
                connection.execute("INSERT INTO borrowed_trace_rows VALUES (23)")
                if fail:
                    raise failure

        return await asyncio.to_thread(timing.sync("database_admission", work))

    try:
        with trace_api(timing):
            if fail:
                with pytest.raises(RuntimeError) as caught:
                    asyncio.run(timing.wrap(app)(
                        {"type": "http", "method": "POST",
                         "path": "/api/v1/recommendations",
                         "headers": [(b"x-evorec-diagnostic-sample", b"0")]},
                        None, None))
                assert caught.value is failure
            else:
                asyncio.run(timing.wrap(app)(
                    {"type": "http", "method": "POST",
                     "path": "/api/v1/recommendations",
                     "headers": [(b"x-evorec-diagnostic-sample", b"0")]},
                    None, None))

        assert psycopg.Transaction.__exit__ is original_transaction_exit
        assert connection.closed is False
        assert connection.info.transaction_status == TransactionStatus.IDLE

        stages = timing.report()[0]["stages"]
        exit_stage = "database_admission_database_transaction_context_exit"
        assert [event for event in stages if event["stage"] == exit_stage]
        assert not any(event["stage"] in {
            "database_admission_database_connect",
            "database_admission_database_close",
        } for event in stages)
        assert "PRIVATE-FAILURE-MARKER" not in json.dumps(timing.report())

        with psycopg.connect(isolated_database) as observer:
            values = observer.execute(
                "SELECT value FROM borrowed_trace_rows ORDER BY value").fetchall()
        assert values == ([] if fail else [(23,)])
    finally:
        connection.close()
    assert psycopg.Transaction.__exit__ is original_transaction_exit
