"""Fresh admission must reuse its execution lease connection."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event, RLock
from uuid import uuid4

import pytest
from psycopg.errors import RaiseException
from psycopg.pq import TransactionStatus

from evorec.bootstrap import build_demo_application
from evorec.domain.errors import IdempotencyInProgress, ManagementError
from evorec.domain.models import RecommendationCommand, Strategy
from evorec.infrastructure.postgres import PostgresDemoBackend
from evorec.infrastructure.recommendation_execution import RecommendationExecution
from scripts.seed_demo_catalog import main as seed_demo_catalog


@pytest.fixture
def admission_connection(isolated_database, monkeypatch):
    monkeypatch.delenv("EVOREC_BUNDLE_ROOT", raising=False)
    seed_demo_catalog()
    backend = PostgresDemoBackend(isolated_database, r06_enabled=False)
    session = asyncio.run(backend.create_session())
    command = RecommendationCommand(uuid4(), session.snapshot.session_id, session.access_token,
                                    0, Strategy.POPULAR, 2, 2.)
    try:
        yield backend, command
    finally:
        asyncio.run(backend.aclose())
        assert not backend._executions


def _request_row(backend, request_id):
    with backend._connect() as connection:
        return connection.execute(
            "SELECT status, execution_owner FROM recommendation_requests WHERE request_id=%s",
            (request_id,),
        ).fetchone()


def _idle(connection):
    assert connection.info.transaction_status == TransactionStatus.IDLE


class _ConnectionProxy:
    """Narrow SQL interception while preserving psycopg connection behavior."""

    def __init__(self, connection, after_execute):
        self.connection = connection
        self.after_execute = after_execute

    @property
    def autocommit(self):
        return self.connection.autocommit

    @autocommit.setter
    def autocommit(self, value):
        self.connection.autocommit = value

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, query, params=None):
        result = self.connection.execute(query, params)
        self.after_execute(query, result)
        return result


class _ObservedRLock:
    """Keep RLock semantics while making a real failed nonblocking acquire observable."""

    def __init__(self):
        self._lock = RLock()
        self.contended = Event()

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self.contended.set()
            self._lock.acquire()
        return self

    def __exit__(self, *_args):
        self._lock.release()


def test_fresh_admission_commits_on_lease_connection_and_releases_row_locks(admission_connection, monkeypatch):
    backend, command = admission_connection
    with backend._connect() as connection:
        connection.execute("CREATE TABLE admission_insert_pid (request_id uuid, backend_pid integer)")
        connection.execute("""
            CREATE FUNCTION record_admission_insert_pid() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                INSERT INTO admission_insert_pid VALUES (NEW.request_id, pg_backend_pid());
                RETURN NEW;
            END $$
        """)
        connection.execute("""
            CREATE TRIGGER record_admission_insert_pid AFTER INSERT ON recommendation_requests
            FOR EACH ROW EXECUTE FUNCTION record_admission_insert_pid()
        """)

    capture_pids, capture_connections = [], []
    original_capture, original_connect = backend._capture_context, backend._connect
    connect_pids = []

    def capture(connection, *args):
        capture_pids.append(connection.info.backend_pid)
        capture_connections.append(connection)
        return original_capture(connection, *args)

    def tracked_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connect_pids.append(connection.info.backend_pid)
        return connection

    monkeypatch.setattr(backend, "_capture_context", capture)
    monkeypatch.setattr(backend, "_connect", tracked_connect)
    execution = RecommendationExecution(backend, command)
    original_assert_held = execution.assert_held
    registry_checks = []

    def assert_registered_and_held():
        assert backend._execution(command.request_id) is execution
        registry_checks.append(True)
        original_assert_held()

    execution.assert_held = assert_registered_and_held
    try:
        context = execution.admit()
        assert context.session.session_id == command.session_id
        assert len(connect_pids) == 1, "admission opened a second connection after the lease connection"
        assert capture_pids == connect_pids
        assert capture_connections == [execution.connection]
        assert registry_checks
        _idle(execution.connection)
        execution.assert_held()

        # The committed row and trigger observation are visible to an independent backend.
        with original_connect() as observer:
            row = observer.execute(
                "SELECT r.status, p.backend_pid FROM recommendation_requests r "
                "JOIN admission_insert_pid p USING (request_id) WHERE r.request_id=%s",
                (command.request_id,),
            ).fetchone()
            assert row == {"status": "accepted", "backend_pid": capture_pids[0]}
            # Admission's session and catalog FOR SHARE locks ended at commit.
            observer.execute("SELECT session_id FROM sessions WHERE session_id=%s FOR UPDATE NOWAIT",
                             (command.session_id,))
            observer.execute("SELECT singleton FROM catalog_control WHERE singleton=1 FOR UPDATE NOWAIT")
            assert not observer.execute("SELECT pg_try_advisory_lock(%s) AS held",
                                        (execution.lock_key,)).fetchone()["held"]
    finally:
        execution.close()

    with original_connect() as observer:
        held = observer.execute("SELECT pg_try_advisory_lock(%s) AS held",
                                (execution.lock_key,)).fetchone()["held"]
        assert held is True
        observer.execute("SELECT pg_advisory_unlock(%s)", (execution.lock_key,))


def test_capture_exception_rolls_back_without_losing_idle_lease(admission_connection, monkeypatch):
    backend, command = admission_connection
    execution = RecommendationExecution(backend, command)
    original_capture = backend._capture_context
    failure = RuntimeError("capture failed after reading real state")

    def fail_after_capture(connection, *args):
        original_capture(connection, *args)
        raise failure

    monkeypatch.setattr(backend, "_capture_context", fail_after_capture)
    try:
        with pytest.raises(RuntimeError, match="after reading real state") as error:
            execution.admit()
        assert error.value is failure
        assert not execution.admitted
        assert _request_row(backend, command.request_id) is None
        _idle(execution.connection)
        execution.assert_held()
    finally:
        execution.close()


def test_insert_trigger_error_rolls_back_row_and_keeps_lease_idle(admission_connection):
    backend, command = admission_connection
    with backend._connect() as connection:
        connection.execute("""
            CREATE FUNCTION reject_admission_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'deliberate admission trigger failure';
            END $$
        """)
        connection.execute("""
        CREATE CONSTRAINT TRIGGER reject_admission_insert AFTER INSERT ON recommendation_requests
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_admission_insert()
        """)

    execution = RecommendationExecution(backend, command)
    try:
        with pytest.raises(RaiseException, match="deliberate admission trigger failure"):
            execution.admit()
        assert not execution.admitted
        assert _request_row(backend, command.request_id) is None
        _idle(execution.connection)
        execution.assert_held()
    finally:
        execution.close()


@pytest.mark.parametrize("invalid_state", ["autocommit_off", "outer_transaction", "closed", "unlocked"])
def test_invalid_execution_connection_cannot_admit(admission_connection, invalid_state):
    backend, command = admission_connection
    execution = RecommendationExecution(backend, command)
    connection = backend._connect()
    connection.autocommit = True
    execution.connection = connection
    execution.lock_key = connection.execute(
        "SELECT hashtextextended('evorec:recommendation:' || current_schema() || ':' || %s, 0) AS key",
        (str(command.request_id),),
    ).fetchone()["key"]
    connection.execute("SELECT pg_advisory_lock(%s)", (execution.lock_key,))
    with backend._execution_lock:
        backend._executions[command.request_id] = execution

    try:
        if invalid_state == "autocommit_off":
            connection.autocommit = False
        elif invalid_state == "outer_transaction":
            connection.execute("BEGIN")
            assert connection.autocommit
            assert connection.info.transaction_status == TransactionStatus.INTRANS
        elif invalid_state == "closed":
            connection.close()
        elif invalid_state == "unlocked":
            connection.execute("SELECT pg_advisory_unlock(%s)", (execution.lock_key,))

        with pytest.raises(ManagementError):
            backend._admit(command, execution=execution)
        assert _request_row(backend, command.request_id) is None
    finally:
        execution.close()


def test_stale_prelock_observation_reconciles_row_created_before_lease(admission_connection, monkeypatch):
    backend, command = admission_connection
    other = PostgresDemoBackend(backend.database_url, r06_enabled=False)
    original_connect = backend._connect
    raced = Event()

    winner_owners = []

    def racing_connect(*args, **kwargs):
        def after_execute(query, _result):
            if not raced.is_set() and "LEFT JOIN recommendation_requests" in query:
                # Publish and close a genuine accepted execution after the first
                # connection observed no row, but before it tries the lease.
                raced.set()
                winner = RecommendationExecution(other, command)
                try:
                    winner.admit()
                    winner_owners.append(winner.owner)
                finally:
                    winner.close()

        return _ConnectionProxy(original_connect(*args, **kwargs), after_execute)

    monkeypatch.setattr(backend, "_connect", racing_connect)
    execution = RecommendationExecution(backend, command)
    try:
        with pytest.raises(ManagementError) as error:
            execution.admit()
        assert error.value.status_code == 409
        assert raced.is_set()
        row = _request_row(backend, command.request_id)
        assert row["status"] == "failed"
        assert row["execution_owner"] == winner_owners[0]
        with original_connect() as observer:
            terminal = observer.execute(
                "SELECT failure_code FROM recommendation_requests WHERE request_id=%s",
                (command.request_id,),
            ).fetchone()
            assert terminal["failure_code"] == "execution_interrupted"
            assert observer.execute("SELECT count(*) AS n FROM request_items WHERE request_id=%s",
                                    (command.request_id,)).fetchone()["n"] == 0
    finally:
        execution.close()
        asyncio.run(other.aclose())


@pytest.mark.parametrize("injection_point", ["after_probe", "after_previous"])
def test_request_appearing_after_lease_probe_rolls_back_then_reconciles(
        admission_connection, monkeypatch, injection_point):
    backend, command = admission_connection
    other = PostgresDemoBackend(backend.database_url, r06_enabled=False)
    template_command = replace(command, request_id=uuid4())
    template = RecommendationExecution(other, template_command)
    template.admit()
    template_owner = template.owner
    template.close()

    original_connect = backend._connect
    injected = Event()

    def after_execute(query, _result):
        postprobe = "SELECT EXISTS (SELECT 1 FROM recommendation_requests" in query
        postselect = ("FROM recommendation_requests" in query and "FOR UPDATE" in query)
        should_inject = ((injection_point == "after_probe" and postprobe)
                         or (injection_point == "after_previous" and postselect))
        if not injected.is_set() and should_inject:
            injected.set()
            # Copy a real accepted row through an independent connection, outside
            # the lease protocol, after the borrowed transaction's check returned.
            with other._connect() as clone:
                clone.execute(
                    "INSERT INTO recommendation_requests (request_id, session_id, session_epoch, "
                    "history_version, history_snapshot, hidden_snapshot, bundle_id, exclusion_version, "
                    "requested_strategy, requested_k, model_snapshot, execution_owner, status) "
                    "SELECT %s, session_id, session_epoch, history_version, history_snapshot, "
                    "hidden_snapshot, bundle_id, exclusion_version, requested_strategy, requested_k, "
                    "model_snapshot, execution_owner, status FROM recommendation_requests "
                    "WHERE request_id=%s",
                    (command.request_id, template_command.request_id),
                )

    monkeypatch.setattr(backend, "_connect", lambda *args, **kwargs:
                        _ConnectionProxy(original_connect(*args, **kwargs), after_execute))
    execution = RecommendationExecution(backend, command)
    try:
        with pytest.raises(ManagementError) as error:
            execution.admit()
        assert error.value.status_code == 409
        assert injected.is_set()
        row = _request_row(backend, command.request_id)
        assert row["status"] == "failed"
        assert row["execution_owner"] == template_owner
        with original_connect() as observer:
            terminal = observer.execute(
                "SELECT failure_code FROM recommendation_requests WHERE request_id=%s",
                (command.request_id,),
            ).fetchone()
            assert terminal["failure_code"] == "execution_interrupted"
            assert observer.execute("SELECT count(*) AS n FROM request_items WHERE request_id=%s",
                                    (command.request_id,)).fetchone()["n"] == 0
    finally:
        execution.close()
        asyncio.run(other.aclose())


@pytest.mark.parametrize("invalid_lease", ["backend", "request", "unregistered"])
def test_admission_rejects_mismatched_or_unregistered_execution_lease(admission_connection, invalid_lease):
    backend, command = admission_connection
    other = PostgresDemoBackend(backend.database_url, r06_enabled=False)
    execution = RecommendationExecution(backend, command)
    connection = backend._connect()
    connection.autocommit = True
    execution.connection = connection
    execution.lock_key = connection.execute(
        "SELECT hashtextextended('evorec:recommendation:' || current_schema() || ':' || %s, 0) AS key",
        (str(command.request_id),),
    ).fetchone()["key"]
    connection.execute("SELECT pg_advisory_lock(%s)", (execution.lock_key,))
    with backend._execution_lock:
        backend._executions[command.request_id] = execution

    try:
        target_backend, target_command = backend, command
        if invalid_lease == "backend":
            target_backend = other
        elif invalid_lease == "request":
            target_command = replace(command, request_id=uuid4())
        else:
            with backend._execution_lock:
                del backend._executions[command.request_id]

        with pytest.raises(ManagementError):
            target_backend._admit(target_command, execution=execution)
        assert _request_row(backend, command.request_id) is None
    finally:
        execution.close()
        asyncio.run(other.aclose())


def test_fresh_admission_requires_the_registered_execution_argument(admission_connection, monkeypatch):
    backend, command = admission_connection
    execution = RecommendationExecution(backend, command)
    connection = backend._connect()
    connection.autocommit = True
    execution.connection = connection
    execution.lock_key = connection.execute(
        "SELECT hashtextextended('evorec:recommendation:' || current_schema() || ':' || %s, 0) AS key",
        (str(command.request_id),),
    ).fetchone()["key"]
    connection.execute("SELECT pg_advisory_lock(%s)", (execution.lock_key,))
    with backend._execution_lock:
        backend._executions[command.request_id] = execution
    capture_calls = []
    original_capture = backend._capture_context

    def observe_capture(*args):
        capture_calls.append(True)
        return original_capture(*args)

    monkeypatch.setattr(backend, "_capture_context", observe_capture)
    try:
        with pytest.raises(ManagementError) as error:
            backend._admit(command)
        assert error.value.status_code == 503
        assert not capture_calls
        assert _request_row(backend, command.request_id) is None
        assert backend._execution(command.request_id) is execution
        _idle(connection)
        execution.assert_held()
    finally:
        execution.close()


def test_repeated_cancellation_drains_borrowed_admission_before_releasing_lease(
        admission_connection, monkeypatch):
    backend, command = admission_connection
    command = replace(command, timeout_seconds=20.)
    application = build_demo_application(backend)
    entered, release = Event(), Event()
    original_capture = backend._capture_context

    def gated_capture(connection, *args):
        context = original_capture(connection, *args)
        entered.set()
        assert release.wait(10), "admission capture was not released"
        return context

    monkeypatch.setattr(backend, "_capture_context", gated_capture)

    async def run():
        task = asyncio.create_task(application.recommend.execute(command))
        try:
            assert await asyncio.to_thread(entered.wait, 5), "capture did not reach the real database gate"
            execution = backend._execution(command.request_id)
            assert execution is not None
            owner_connection = execution.connection
            assert owner_connection.info.transaction_status == TransactionStatus.INTRANS
            with backend._connect() as observer:
                assert observer.execute(
                    "SELECT count(*) AS n FROM recommendation_requests WHERE request_id=%s",
                    (command.request_id,),
                ).fetchone()["n"] == 0
                assert not observer.execute("SELECT pg_try_advisory_lock(%s) AS held",
                                            (execution.lock_key,)).fetchone()["held"]

                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
                assert task.cancelling() >= 3
                assert not task.done()
                assert not owner_connection.closed
                assert owner_connection.info.transaction_status == TransactionStatus.INTRANS
                assert not observer.execute("SELECT pg_try_advisory_lock(%s) AS held",
                                            (execution.lock_key,)).fetchone()["held"]
                assert backend._execution(command.request_id) is execution

            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            row = _request_row(backend, command.request_id)
            assert row["status"] == "failed"
            with backend._connect() as observer:
                terminal = observer.execute(
                    "SELECT failure_code FROM recommendation_requests WHERE request_id=%s",
                    (command.request_id,),
                ).fetchone()
                assert terminal["failure_code"] == "execution_failed"
                assert observer.execute("SELECT count(*) AS n FROM request_items WHERE request_id=%s",
                                        (command.request_id,)).fetchone()["n"] == 0
                assert observer.execute("SELECT pg_try_advisory_lock(%s) AS held",
                                        (execution.lock_key,)).fetchone()["held"]
                observer.execute("SELECT pg_advisory_unlock(%s)", (execution.lock_key,))
            assert backend._execution(command.request_id) is None
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_same_registered_execution_serializes_two_real_admission_transactions(
        admission_connection, monkeypatch):
    backend, command = admission_connection
    execution = RecommendationExecution(backend, command)
    connection = backend._connect()
    connection.autocommit = True
    execution.connection = connection
    execution.lock_key = connection.execute(
        "SELECT hashtextextended('evorec:recommendation:' || current_schema() || ':' || %s, 0) AS key",
        (str(command.request_id),),
    ).fetchone()["key"]
    connection.execute("SELECT pg_advisory_lock(%s)", (execution.lock_key,))
    with backend._execution_lock:
        backend._executions[command.request_id] = execution

    capture_entered, release_capture, second_requested = Event(), Event(), Event()
    observed_lock = _ObservedRLock()
    execution._connection_lock = observed_lock
    capture_count = 0
    original_capture = backend._capture_context
    original_admission_connection = execution.admission_connection
    admission_call_count = 0

    def gated_capture(connection, *args):
        nonlocal capture_count
        context = original_capture(connection, *args)
        capture_count += 1
        capture_entered.set()
        assert release_capture.wait(10), "first admission capture was not released"
        return context

    def tracked_admission_connection():
        nonlocal admission_call_count
        admission_call_count += 1
        if admission_call_count == 2:
            second_requested.set()
        return original_admission_connection()

    monkeypatch.setattr(backend, "_capture_context", gated_capture)
    monkeypatch.setattr(execution, "admission_connection", tracked_admission_connection)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(backend._admit, command, execution=execution)
            try:
                assert capture_entered.wait(5), "first admission did not reach capture"
                assert execution.connection.info.transaction_status == TransactionStatus.INTRANS
                second = pool.submit(backend._admit, command, execution=execution)
                assert second_requested.wait(5), "second admission did not reach lease serialization"
                assert observed_lock.contended.wait(5), "second admission never contended on the real owner lock"
                assert capture_count == 1
                assert not first.done() and not second.done()
            finally:
                release_capture.set()
            first_context = first.result(timeout=5)
            assert first_context.session.session_id == command.session_id
            with pytest.raises(IdempotencyInProgress):
                second.result(timeout=5)

        row = _request_row(backend, command.request_id)
        assert row == {"status": "accepted", "execution_owner": execution.owner}
        assert capture_count == 1 and admission_call_count == 2
        assert execution.connection.info.transaction_status == TransactionStatus.IDLE
        execution.assert_held()
        assert backend._execution(command.request_id) is execution
    finally:
        release_capture.set()
        execution.close()
