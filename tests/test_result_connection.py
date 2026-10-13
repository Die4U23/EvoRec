"""Real result transactions borrow only the still-owned execution lease."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest
from psycopg.errors import RaiseException
from psycopg.pq import TransactionStatus

from evorec.domain.errors import ManagementError, SnapshotMismatch
from evorec.domain.models import RecommendationResult
from evorec.domain.recommendation import select_results
from evorec.infrastructure.recommendation_execution import RecommendationExecution
from test_admission_connection import admission_connection, _ObservedRLock


@pytest.fixture
def result_connection(admission_connection):
    backend, command = admission_connection
    execution = RecommendationExecution(backend, command)
    try:
        context = execution.admit()
        batch = asyncio.run(backend.rank(context, command))
        result = RecommendationResult(context.binding, command.strategy, batch.actual_strategy,
                                      select_results(batch.candidates, context, command.k), batch.fallback_reason)
        yield backend, execution, result
    finally:
        execution.close()


def _state(connect, request_id):
    with connect() as observer:
        row = observer.execute(
            "SELECT status, failure_code, execution_owner FROM recommendation_requests WHERE request_id=%s",
            (request_id,),
        ).fetchone()
        count = observer.execute("SELECT count(*) AS n FROM request_items WHERE request_id=%s",
                                 (request_id,)).fetchone()["n"]
    return row, count


def test_result_commits_on_lease_pid_without_new_connection(result_connection, monkeypatch):
    backend, execution, result = result_connection
    connect = backend._connect
    with connect() as observer:
        observer.execute("CREATE TABLE result_pids (request_id uuid, backend_pid integer)")
        observer.execute("""
            CREATE FUNCTION record_result_pid() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                INSERT INTO result_pids VALUES (NEW.request_id, pg_backend_pid());
                RETURN NEW;
            END $$
        """)
        observer.execute("""CREATE TRIGGER record_item_pid AFTER INSERT ON request_items
                         FOR EACH ROW EXECUTE FUNCTION record_result_pid()""")
        observer.execute("""CREATE TRIGGER record_completed_pid AFTER UPDATE ON recommendation_requests
                         FOR EACH ROW WHEN (NEW.status = 'completed') EXECUTE FUNCTION record_result_pid()""")
    new_connections = []

    def tracked_connect():
        connection = connect()
        new_connections.append(connection.info.backend_pid)
        return connection

    monkeypatch.setattr(backend, "_connect", tracked_connect)
    backend._save(result)
    assert new_connections == [], "owned result opened a new connection instead of borrowing its lease"
    backend._save(result)  # Same content is a no-op, not another insertion.
    assert new_connections == []
    assert execution.connection.info.transaction_status == TransactionStatus.IDLE
    assert not execution.connection.closed
    execution.assert_held()
    row, count = _state(connect, result.binding.request_id)
    assert row == {"status": "completed", "failure_code": None, "execution_owner": execution.owner}
    assert count == len(result.items) == 2
    with connect() as observer:
        pids = observer.execute("SELECT backend_pid FROM result_pids WHERE request_id=%s",
                                (result.binding.request_id,)).fetchall()
        assert pids == [{"backend_pid": execution.connection.info.backend_pid}] * 3
        observer.execute("SELECT request_id FROM recommendation_requests WHERE request_id=%s FOR UPDATE NOWAIT",
                         (result.binding.request_id,))
        assert not observer.execute("SELECT pg_try_advisory_lock(%s) AS held",
                                    (execution.lock_key,)).fetchone()["held"]


def test_commit_trigger_error_rolls_back_items_and_keeps_owned_lease(result_connection):
    backend, execution, result = result_connection
    with backend._connect() as observer:
        observer.execute("""
            CREATE FUNCTION reject_result_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'deliberate result commit failure'; END $$
        """)
        observer.execute("""CREATE CONSTRAINT TRIGGER reject_result_commit
            AFTER INSERT ON request_items DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION reject_result_commit()""")
    with pytest.raises(RaiseException, match="deliberate result commit failure"):
        backend._save(result)
    assert execution.connection.info.transaction_status == TransactionStatus.IDLE
    assert not execution.connection.closed
    execution.assert_held()
    row, count = _state(backend._connect, result.binding.request_id)
    assert row["status"] == "accepted" and row["execution_owner"] == execution.owner and count == 0


def test_connection_closed_after_last_write_cannot_report_success(result_connection, monkeypatch):
    backend, execution, result = result_connection
    connection = execution.connection
    original_execute = connection.execute
    closed_after_update = []

    def close_after_last_write(query, *args, **kwargs):
        cursor = original_execute(query, *args, **kwargs)
        if "SET actual_strategy = %s, status = 'completed'" in query:
            closed_after_update.append(True)
            connection.close()  # Real socket closure, not a fake commit error.
        return cursor

    monkeypatch.setattr(connection, "execute", close_after_last_write)
    failure = None
    try:
        backend._save(result)
    except ManagementError as error:
        failure = error
    assert closed_after_update == [True] and connection.closed
    row, count = _state(backend._connect, result.binding.request_id)
    assert row["status"] == "accepted" and count == 0
    assert failure is not None, "closed result lease returned success while database remained accepted with zero items"
    assert failure.code == "recommendation_execution_lost"


@pytest.mark.parametrize("loss", ["missing", "closed", "unlock", "autocommit", "unregistered", "unadmitted"])
def test_invalid_lease_rejects_without_fallback_or_output(result_connection, monkeypatch, loss):
    backend, execution, result = result_connection
    connect, connection = backend._connect, execution.connection
    if loss == "missing": execution.connection = None
    elif loss == "closed": connection.close()
    elif loss == "unlock": connection.execute("SELECT pg_advisory_unlock_all()")
    elif loss == "autocommit": connection.autocommit = False
    elif loss == "unregistered": backend._executions.clear()
    else: execution.admitted = False
    try:
        if loss == "unregistered":
            # No registered executor uses the legacy path, but the durable
            # owner guard must still reject this owned accepted row.
            with pytest.raises(SnapshotMismatch): backend._save(result)
        else:
            def no_connect():
                pytest.fail("invalid registered result lease silently fell back to a new connection")
            monkeypatch.setattr(backend, "_connect", no_connect)
            with pytest.raises(ManagementError) as failure: backend._save(result)
            assert failure.value.code == "recommendation_execution_lost"
        row, count = _state(connect, result.binding.request_id)
        assert row["status"] == "accepted" and count == 0
    finally:
        execution.connection = connection


def test_outer_transaction_is_not_committed_or_rolled_back_by_result(result_connection):
    backend, execution, result = result_connection
    with execution.connection.transaction():
        execution.connection.execute("CREATE TABLE outer_result_marker (value integer)")
        with pytest.raises(ManagementError): backend._save(result)
        assert execution.connection.info.transaction_status == TransactionStatus.INTRANS
        assert execution.connection.execute("SELECT count(*) AS n FROM outer_result_marker").fetchone()["n"] == 0
        row, count = _state(backend._connect, result.binding.request_id)
        assert row["status"] == "accepted" and count == 0
    with backend._connect() as observer:
        assert observer.execute("SELECT count(*) AS n FROM outer_result_marker").fetchone()["n"] == 0
    execution.assert_held()


@pytest.mark.parametrize("mismatch", ["backend", "request", "owner", "owner_missing", "binding", "model"])
def test_mismatched_result_identity_or_owner_cannot_commit(result_connection, mismatch):
    backend, execution, result = result_connection
    original_backend, original_command = execution.backend, execution.command
    if mismatch == "backend": execution.backend = object()
    elif mismatch == "request": execution.command = replace(execution.command, request_id=uuid4())
    elif mismatch in {"owner", "owner_missing"}:
        with backend._connect() as observer:
            observer.execute("UPDATE recommendation_requests SET execution_owner=%s WHERE request_id=%s",
                             (uuid4() if mismatch == "owner" else None, result.binding.request_id))
    elif mismatch == "binding": result = replace(result, binding=replace(result.binding, history_version=1))
    else: result = replace(result, model_version="a" * 64, captured_at_ms=123)
    try:
        with pytest.raises(SnapshotMismatch): backend._save(result)
        row, count = _state(backend._connect, result.binding.request_id)
        assert row["status"] == "accepted" and count == 0
        assert execution.connection.info.transaction_status == TransactionStatus.IDLE
    finally:
        execution.backend, execution.command = original_backend, original_command


def test_completed_result_remains_unique_and_legacy_replay_uses_owned_connection(result_connection, monkeypatch):
    backend, execution, result = result_connection
    backend._save(result)
    with pytest.raises(SnapshotMismatch):
        backend._save(replace(result, items=(replace(result.items[0], score=0.25),)))
    execution.close()
    connect, opened = backend._connect, []

    def tracked():
        connection = connect()
        opened.append(connection)
        return connection

    monkeypatch.setattr(backend, "_connect", tracked)
    backend._save(result)
    assert len(opened) == 1 and opened[0].closed
    backend._mark_failed(result.binding.request_id, execution.owner)
    row, count = _state(connect, result.binding.request_id)
    assert row["status"] == "completed" and count == 2


@pytest.mark.parametrize("after_commit", [False, True])
def test_close_waits_for_actual_result_transaction_exit(result_connection, monkeypatch, after_commit):
    backend, execution, result = result_connection
    original_transaction = execution.connection.transaction
    entered, release = Event(), Event()
    observed_lock = _ObservedRLock()
    execution._connection_lock = observed_lock

    @contextmanager
    def gated_transaction():
        with original_transaction():
            yield
            if not after_commit:
                entered.set()
                assert release.wait(10), "result transaction was not released before commit"
        if after_commit:
            entered.set()
            assert release.wait(10), "result transaction was not released after commit"

    monkeypatch.setattr(execution.connection, "transaction", gated_transaction)
    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(backend._save, result)
        try:
            assert entered.wait(5)
            close = pool.submit(execution.close)
            assert observed_lock.contended.wait(5), "close never contended on the transaction owner lock"
            assert not close.done() and not execution.connection.closed
            with backend._connect() as observer:
                assert not observer.execute("SELECT pg_try_advisory_lock(%s) AS held",
                                            (execution.lock_key,)).fetchone()["held"]
            row, count = _state(backend._connect, result.binding.request_id)
            assert (row["status"], count) == (("completed", 2) if after_commit else ("accepted", 0))
        finally:
            release.set()
        write.result(timeout=5)
        close.result(timeout=5)
    assert execution.connection.closed and not backend._executions
    row, count = _state(backend._connect, result.binding.request_id)
    assert row["status"] == "completed" and count == 2


def test_two_result_transactions_serialize_and_insert_once(result_connection, monkeypatch):
    backend, execution, result = result_connection
    original_transaction = execution.connection.transaction
    entered, release = Event(), Event()
    observed_lock = _ObservedRLock()
    execution._connection_lock = observed_lock

    @contextmanager
    def gated_transaction():
        with original_transaction():
            yield
            if not entered.is_set():
                entered.set()
                assert release.wait(10), "first result transaction was not released"

    monkeypatch.setattr(execution.connection, "transaction", gated_transaction)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(backend._save, result)
        try:
            assert entered.wait(5)
            second = pool.submit(backend._save, result)
            assert observed_lock.contended.wait(5), "second result did not serialize with the first transaction"
            assert not first.done() and not second.done()
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    row, count = _state(backend._connect, result.binding.request_id)
    assert row["status"] == "completed" and count == 2
    assert execution.connection.info.transaction_status == TransactionStatus.IDLE
    execution.assert_held()
