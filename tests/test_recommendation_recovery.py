"""Real database execution-lock and terminal crash reconciliation, not reranking."""

import asyncio
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from threading import Event, Lock
import time
from uuid import uuid4

import httpx
import pytest

from evorec.api.app import create_app
from evorec.bootstrap import build_demo_application
from evorec.domain.errors import IdempotencyInProgress, ManagementError, SnapshotMismatch
from evorec.domain.models import RecommendationCommand, RecommendationResult, Strategy
from evorec.domain.recommendation import select_results
from evorec.infrastructure.postgres import PostgresDemoBackend
import evorec.infrastructure.postgres as postgres_adapter
from evorec.infrastructure.recommendation_execution import RecommendationExecution
from scripts.seed_demo_catalog import main as seed_demo_catalog
from test_r06_online import online, _command


@pytest.fixture
def recovery(isolated_database, monkeypatch):
    monkeypatch.delenv("EVOREC_BUNDLE_ROOT", raising=False)
    seed_demo_catalog()
    backend = PostgresDemoBackend(isolated_database, r06_enabled=True)
    application = build_demo_application(backend)
    session = asyncio.run(backend.create_session())
    command = RecommendationCommand(uuid4(), session.snapshot.session_id, session.access_token,
                                    0, Strategy.POPULAR, 2, 20.)
    try:
        yield application, command
    finally:
        asyncio.run(backend.aclose())
        assert not backend._executions


def state(backend, command):
    with backend._connect() as c:
        return c.execute("SELECT status, failure_code, execution_owner FROM recommendation_requests "
                         "WHERE request_id = %s", (command.request_id,)).fetchone()


def assert_unlocked(backend, command):
    with backend._connect() as c:
        assert c.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended('evorec:recommendation:' "
            "|| current_schema() || ':' || %s, 0)) AS held", (str(command.request_id),),
        ).fetchone()["held"]


async def post(application, command):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=application)),
                                 base_url="http://isolated") as client:
        return await client.post("/api/v1/recommendations", headers={
            "X-Session-Token": command.session_token, "Idempotency-Key": str(command.request_id),
        }, json=dict(session_id=str(command.session_id), expected_history_version=command.expected_history_version,
                     strategy=command.strategy.value, k=command.k))


async def started(event):
    async with asyncio.timeout(5):
        while not event.is_set():
            await asyncio.sleep(.01)


@pytest.mark.parametrize("worker_fails", [False, True])
@pytest.mark.parametrize("trigger", ["repeated_cancel", "deadline"])
def test_readiness_cancellation_drains_real_connection_before_return(recovery, monkeypatch, worker_fails, trigger):
    application, command = recovery
    backend = application.backend
    manager = backend.manager
    entered, release, exited = Event(), Event(), Event()
    connections, allocations = [], []
    failure = ManagementError("readiness_probe_failed", "intentional readiness failure", 503)
    original_execution = postgres_adapter.RecommendationExecution

    def tracked_execution(*args, **kwargs):
        execution = original_execution(*args, **kwargs)
        allocations.append(execution)
        return execution

    def gated_readiness():
        try:
            with manager._connect(autocommit=True) as connection:
                connections.append(connection)
                connection.execute(f"SELECT pg_advisory_lock({manager.LOCK_KEY_SQL})", (manager.LOCK_NAME,))
                try:
                    entered.set()
                    assert release.wait(10), "readiness probe was not released"
                    if worker_fails:
                        raise failure
                finally:
                    # Match recovery's acknowledged unlock; closing the worker socket is not the release operation.
                    unlocked = connection.execute(
                        f"SELECT pg_advisory_unlock({manager.LOCK_KEY_SQL}) AS unlocked",
                        (manager.LOCK_NAME,),
                    ).fetchone()["unlocked"]
                    assert unlocked is True
        finally:
            exited.set()

    monkeypatch.setattr(manager, "ensure_ready", gated_readiness)
    monkeypatch.setattr(postgres_adapter, "RecommendationExecution", tracked_execution)

    async def run():
        loop = asyncio.get_running_loop()
        unhandled = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        timed_command = replace(command, timeout_seconds=2.) if trigger == "deadline" else command
        task = asyncio.create_task(application.recommend.execute(timed_command))
        try:
            await started(entered)
            owner = connections[0]
            with manager._connect(autocommit=True) as observer:
                if trigger == "repeated_cancel":
                    for _ in range(3):
                        task.cancel()
                        await asyncio.sleep(.01)
                else:
                    async with asyncio.timeout(5):
                        while not task.done() and task.cancelling() == 0:
                            await asyncio.sleep(.01)
                assert not task.done() and not exited.is_set()
                assert not owner.closed
                assert not observer.execute(
                    f"SELECT pg_try_advisory_xact_lock({manager.LOCK_KEY_SQL}) AS held",
                    (manager.LOCK_NAME,),
                ).fetchone()["held"]
                assert not allocations and not backend._executions
                assert state(backend, command) is None
                release.set()
                if trigger == "deadline":
                    with pytest.raises(TimeoutError):
                        await task
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await task
                assert exited.is_set() and owner.closed
                assert observer.execute(
                    f"SELECT pg_try_advisory_xact_lock({manager.LOCK_KEY_SQL}) AS held",
                    (manager.LOCK_NAME,),
                ).fetchone()["held"]
            assert not allocations and not backend._executions
            assert state(backend, command) is None
            await asyncio.sleep(0)
            assert not unhandled
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            try:
                if connections:
                    await started(exited)
                    assert all(connection.closed for connection in connections)
            finally:
                loop.set_exception_handler(previous_handler)

    asyncio.run(run())


def test_readiness_worker_failure_propagates_unchanged_before_execution_allocation(recovery, monkeypatch):
    application, command = recovery
    backend = application.backend
    manager = backend.manager
    failure = ManagementError("readiness_probe_failed", "intentional readiness failure", 503)
    connections, allocations = [], []
    original_execution = postgres_adapter.RecommendationExecution

    def failed_readiness():
        with manager._connect(autocommit=True) as connection:
            connections.append(connection)
            connection.execute(f"SELECT pg_advisory_lock({manager.LOCK_KEY_SQL})", (manager.LOCK_NAME,))
            try:
                raise failure
            finally:
                # Match recovery's acknowledged unlock; closing the worker socket is not the release operation.
                unlocked = connection.execute(
                    f"SELECT pg_advisory_unlock({manager.LOCK_KEY_SQL}) AS unlocked",
                    (manager.LOCK_NAME,),
                ).fetchone()["unlocked"]
                assert unlocked is True

    def tracked_execution(*args, **kwargs):
        execution = original_execution(*args, **kwargs)
        allocations.append(execution)
        return execution

    monkeypatch.setattr(manager, "ensure_ready", failed_readiness)
    monkeypatch.setattr(postgres_adapter, "RecommendationExecution", tracked_execution)
    with pytest.raises(ManagementError) as caught:
        asyncio.run(application.recommend.execute(command))
    assert caught.value is failure
    assert connections and all(connection.closed for connection in connections)
    assert not allocations and not backend._executions
    assert state(backend, command) is None
    with manager._connect(autocommit=True) as observer:
        assert observer.execute(
            f"SELECT pg_try_advisory_xact_lock({manager.LOCK_KEY_SQL}) AS held",
            (manager.LOCK_NAME,),
        ).fetchone()["held"]


def test_actual_process_kill_reconciles_without_reranking_and_allows_explicit_new_key(recovery, tmp_path):
    application, command = recovery
    marker = tmp_path / "actual-recommendation-ranking"
    # No connection string/token in source, argv, marker or child output.
    source = (
        "import asyncio,json,os,sys,time\n"
        "from pathlib import Path\n"
        "from uuid import UUID\n"
        "from evorec.bootstrap import build_demo_application\n"
        "from evorec.infrastructure.postgres import PostgresDemoBackend\n"
        "from evorec.domain.models import RecommendationCommand,Strategy\n"
        "data=json.load(sys.stdin)\n"
        "b=PostgresDemoBackend(os.environ['EVOREC_DATABASE_URL'],r06_enabled=True)\n"
        "a=build_demo_application(b)\n"
        "def work():\n"
        f"    Path({str(marker)!r}).touch()\n"
        "    time.sleep(120)\n"
        "async def rank(context,command): return await b.r06_queue.run(work)\n"
        "b.rank=rank\n"
        "c=RecommendationCommand(UUID(data['id']),UUID(data['session']),data['token'],0,Strategy.POPULAR,2,120.)\n"
        "asyncio.run(a.recommend.execute(c))\n"
    )
    import json
    child = subprocess.Popen([sys.executable, "-u", "-c", source], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        child.stdin.write(json.dumps(dict(id=str(command.request_id), session=str(command.session_id),
                                         token=command.session_token)))
        child.stdin.close()
        child.stdin = None
        deadline = time.monotonic() + 15
        while not marker.exists():
            assert child.poll() is None, "owned worker exited before ranking"
            assert time.monotonic() < deadline, "owned worker did not enter ranking"
            time.sleep(.02)
        assert state(application.backend, command)["execution_owner"] is not None
        busy = asyncio.run(post(application, command))
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "recommendation_in_progress"
        assert busy.json()["error"]["retryable"]
        # A session reset cannot turn reconciliation into a new-model/history calculation.
        reset = asyncio.run(application.backend.reset_session(command.session_id, command.session_token))
        child.kill()
        child.communicate(timeout=5)
        assert_unlocked(application.backend, command)
        restarted = build_demo_application(PostgresDemoBackend(application.backend.database_url, r06_enabled=False))
        for _ in range(2):
            reply = asyncio.run(post(restarted, command))
            assert reply.status_code == 409 and reply.json()["error"]["code"] == "recommendation_interrupted"
            assert not reply.json()["error"]["retryable"]
        assert state(application.backend, command)["failure_code"] == "execution_interrupted"
        with application.backend._connect() as c:
            assert c.execute("SELECT count(*) AS n FROM request_items WHERE request_id=%s",
                             (command.request_id,)).fetchone()["n"] == 0
        new = replace(command, request_id=uuid4(), expected_history_version=reset.history_version)
        assert asyncio.run(post(restarted, new)).status_code == 200
        assert state(application.backend, new)["status"] == "completed"
        assert not restarted.backend._executions
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


@pytest.mark.parametrize("change", ["token", "session", "history", "strategy", "k"])
def test_orphan_reconciliation_requires_authorized_identical_input(recovery, change):
    application, command = recovery
    execution = RecommendationExecution(application.backend, command)
    execution.admit()
    execution.close()  # Real lost PostgreSQL session lock; leave the accepted row intact.
    if change == "token":
        altered = replace(command, session_token="incorrect")
    elif change == "session":
        another = asyncio.run(application.backend.create_session())
        altered = replace(command, session_id=another.snapshot.session_id, session_token=another.access_token)
    elif change == "history":
        altered = replace(command, expected_history_version=1)
    elif change == "strategy":
        altered = replace(command, strategy=Strategy.DENSE)
    else:
        altered = replace(command, k=3)
    reply = asyncio.run(post(application, altered))
    assert reply.status_code == (401 if change == "token" else 409)
    assert state(application.backend, command)["status"] == "accepted"
    assert asyncio.run(post(application, command)).json()["error"]["code"] == "recommendation_interrupted"
    assert state(application.backend, command)["status"] == "failed"
    assert_unlocked(application.backend, command)


def test_legacy_unmarked_execution_is_not_declared_dead_and_can_complete(recovery):
    application, command = recovery
    context = application.backend._comparison_snapshot(command)
    # An actual old INSERT omits the nullable new owner, including during upgrades.
    with application.backend._connect() as c:
        c.execute(
            "INSERT INTO recommendation_requests (request_id, session_id, session_epoch, history_version, "
            "history_snapshot, hidden_snapshot, bundle_id, exclusion_version, requested_strategy, requested_k, status) "
            "SELECT %s,s.session_id,s.epoch,s.history_version,s.history,s.hidden_items,c.active_bundle_id,"
            "c.exclusion_version,%s,%s,'accepted' FROM sessions s CROSS JOIN catalog_control c "
            "WHERE s.session_id=%s AND c.singleton=1",
            (command.request_id, command.strategy, command.k, command.session_id),
        )
    assert state(application.backend, command)["execution_owner"] is None
    reply = asyncio.run(post(application, command))
    assert reply.status_code == 409 and reply.json()["error"]["code"] == "recommendation_recovery_unavailable"
    assert not reply.json()["error"]["retryable"]
    assert state(application.backend, command)["status"] == "accepted"
    batch = asyncio.run(application.backend.rank(context, command))
    result = RecommendationResult(context.binding, command.strategy, batch.actual_strategy,
                                  select_results(batch.candidates, context, command.k), batch.fallback_reason)
    asyncio.run(application.backend.save(result))  # Old executor compatibility.
    replay = asyncio.run(post(application, command))
    assert replay.status_code == 200, replay.json()
    assert_unlocked(application.backend, command)


@pytest.mark.parametrize("after_commit", [False, True])
def test_repeated_cancel_during_result_write_drains_and_retains_execution_lock(recovery, monkeypatch, after_commit):
    application, command = recovery
    entered, release, exited = Event(), Event(), Event()
    original = application.backend._save
    def blocked(result):
        if after_commit:
            original(result)
        entered.set()
        assert release.wait(5)
        if not after_commit:
            original(result)
        exited.set()
    monkeypatch.setattr(application.backend, "_save", blocked)
    async def run():
        task = asyncio.create_task(application.recommend.execute(command))
        try:
            await started(entered)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(.01)
                assert not task.done() and not exited.is_set()
            reply = await post(application, command)
            if after_commit:
                assert reply.status_code == 200  # Replays even while the old lease is still live.
            else:
                assert reply.json()["error"]["code"] == "recommendation_in_progress"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert exited.is_set()
            assert state(application.backend, command)["status"] == "completed"
            first, second = await post(application, command), await post(application, command)
            assert first.status_code == second.status_code == 200 and first.json() == second.json()
            assert_unlocked(application.backend, command)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("loss", ["close", "unlock", "owner_change"])
def test_lost_or_mismatched_execution_cannot_commit_late_results(recovery, monkeypatch, loss):
    application, command = recovery
    entered, release = Event(), Event()
    original = application.backend.rank
    async def rank(context, cmd):
        def work():
            entered.set()
            assert release.wait(5)
        await application.backend.r06_queue.run(work)
        return await original(context, cmd)
    monkeypatch.setattr(application.backend, "rank", rank)
    async def run():
        task = asyncio.create_task(application.recommend.execute(command))
        try:
            await started(entered)
            execution = application.backend._execution(command.request_id)
            if loss == "close":
                execution.close()
            elif loss == "unlock":
                execution.connection.execute("SELECT pg_advisory_unlock_all()")
            else:
                with application.backend._connect() as c:
                    c.execute("UPDATE recommendation_requests SET execution_owner=%s WHERE request_id=%s",
                              (uuid4(), command.request_id))
            release.set()
            with pytest.raises((SnapshotMismatch, ManagementError)):
                await task
            assert state(application.backend, command)["status"] == "failed"
            with application.backend._connect() as c:
                assert c.execute("SELECT count(*) AS n FROM request_items WHERE request_id=%s",
                                 (command.request_id,)).fetchone()["n"] == 0
            assert_unlocked(application.backend, command)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_live_old_request_not_reclaimed_by_age_or_second_backend(recovery):
    application, command = recovery
    async def run():
        async with application.backend.acquire(command):
            with application.backend._connect() as c:
                c.execute("UPDATE recommendation_requests SET created_at=now()-interval '100 days' WHERE request_id=%s",
                          (command.request_id,))
            other = build_demo_application(PostgresDemoBackend(application.backend.database_url, r06_enabled=False))
            reply = await post(other, command)
            assert reply.json()["error"]["code"] == "recommendation_in_progress"
            assert state(application.backend, command)["status"] == "accepted"
            assert not other.backend._executions
    asyncio.run(run())
    assert_unlocked(application.backend, command)


def test_cpu_cancellation_retains_execution_lock_until_actual_thread_finishes(recovery, monkeypatch):
    application, command = recovery
    entered, release = Event(), Event()
    async def rank(context, cmd):
        def work():
            entered.set()
            assert release.wait(5)
        await application.backend.r06_queue.run(work)
    monkeypatch.setattr(application.backend, "rank", rank)
    async def run():
        task = asyncio.create_task(application.recommend.execute(command))
        try:
            await started(entered)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(.01)
                assert not task.done()
            other = build_demo_application(PostgresDemoBackend(application.backend.database_url, r06_enabled=False))
            assert (await post(other, command)).json()["error"]["code"] == "recommendation_in_progress"
            assert state(application.backend, command)["status"] == "accepted"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert state(application.backend, command)["status"] == "failed"
            assert application.backend.r06_queue.outstanding == 0
            assert_unlocked(application.backend, command)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_late_output_never_overwrites_reconciled_interruption(recovery, monkeypatch):
    application, command = recovery
    entered, release = Event(), Event()
    original = application.backend.rank
    async def rank(context, cmd):
        def work():
            entered.set()
            assert release.wait(5)
        await application.backend.r06_queue.run(work)
        return await original(context, cmd)
    monkeypatch.setattr(application.backend, "rank", rank)
    async def run():
        task = asyncio.create_task(application.recommend.execute(command))
        try:
            await started(entered)
            application.backend._execution(command.request_id).close()
            reply = await post(application, command)
            assert reply.json()["error"]["code"] == "recommendation_interrupted"
            release.set()
            with pytest.raises(SnapshotMismatch):
                await task
            assert state(application.backend, command)["failure_code"] == "execution_interrupted"
            with application.backend._connect() as c:
                assert c.execute("SELECT count(*) AS n FROM request_items WHERE request_id=%s",
                                 (command.request_id,)).fetchone()["n"] == 0
            assert_unlocked(application.backend, command)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_r06_orphan_retains_original_model_snapshot_after_catalog_drift_and_reset(online):
    application, _, _ = online
    command = asyncio.run(_command(application))
    execution = RecommendationExecution(application.backend, command)
    execution.admit()
    execution.close()
    with application.backend._connect() as c:
        frozen = c.execute("SELECT model_snapshot FROM recommendation_requests WHERE request_id=%s",
                           (command.request_id,)).fetchone()["model_snapshot"]
        assert frozen is not None
        c.execute("UPDATE items SET r06_model_text='changed after capture'")
    asyncio.run(application.backend.reset_session(command.session_id, command.session_token))
    assert asyncio.run(post(application, command)).json()["error"]["code"] == "recommendation_interrupted"
    with application.backend._connect() as c:
        row = c.execute("SELECT model_snapshot, status, failure_code FROM recommendation_requests WHERE request_id=%s",
                        (command.request_id,)).fetchone()
        assert row["model_snapshot"] == frozen and row["status"] == "failed"
        assert row["failure_code"] == "execution_interrupted"
    assert_unlocked(application.backend, command)


def test_fresh_admission_without_execution_lease_is_rejected_before_insert(recovery):
    application, command = recovery
    with pytest.raises(ManagementError) as failure:
        application.backend._admit(command)
    assert failure.value.code == "recommendation_execution_lost"
    assert state(application.backend, command) is None


def test_concurrent_orphan_retries_share_one_terminal_outcome_without_execution(recovery, monkeypatch):
    application, command = recovery
    execution = RecommendationExecution(application.backend, command)
    execution.admit()
    execution.close()
    original_owner = state(application.backend, command)["execution_owner"]
    apps = [build_demo_application(PostgresDemoBackend(application.backend.database_url, r06_enabled=False))
            for _ in range(4)]
    # Passive observer overhead is included; these times are not benchmark evidence.
    # Record fixed labels/codes only, never SQL, exception messages or connection data.
    stages, replies = [], []
    stage_lock = Lock()
    indices = {id(app.backend): index for index, app in enumerate(apps)}
    diagnostic_codes = frozenset({"recommendation_interrupted", "recommendation_in_progress",
                                 "recommendation_timeout", "database_unavailable", "catalog_unavailable",
                                 "recommendation_execution_lost", "recommendation_recovery_unavailable",
                                 "recommendation_idempotency_conflict", "session_not_found",
                                 "session_access_denied", "history_conflict"})

    def safe_code(code):
        return code if type(code) is str and code in diagnostic_codes else None

    def record(index, stage, event, error=None):
        entry = (index, stage, event, time.monotonic(),
                 type(error).__name__ if error is not None else None,
                 safe_code(getattr(error, "code", None)))
        with stage_lock:
            stages.append(entry)

    def observed(index, stage, work, *args, **kwargs):
        record(index, stage, "enter")
        try:
            result = work(*args, **kwargs)
        except BaseException as error:
            record(index, stage, "exit", error)
            raise
        record(index, stage, "exit")
        return result

    def wrap_execution(stage, original):
        def wrapped(execution, *args, **kwargs):
            index = indices.get(id(execution.backend))
            if index is None:
                return original(execution, *args, **kwargs)
            return observed(index, stage, original, execution, *args, **kwargs)
        return wrapped

    monkeypatch.setattr(RecommendationExecution, "admit",
                        wrap_execution("lease_admission", RecommendationExecution.admit))
    monkeypatch.setattr(RecommendationExecution, "close",
                        wrap_execution("lease_close", RecommendationExecution.close))

    def wrap_work(index, stage, original):
        def wrapped(*args, **kwargs):
            return observed(index, stage, original, *args, **kwargs)
        return wrapped

    for index, app in enumerate(apps):
        monkeypatch.setattr(app.manager, "ensure_ready", wrap_work(index, "readiness", app.manager.ensure_ready))
        monkeypatch.setattr(app.backend, "_admit", wrap_work(index, "terminal_admission", app.backend._admit))

    async def observed_post(index, app):
        record(index, "http_request", "enter")
        try:
            reply = await post(app, command)
        except BaseException as error:
            record(index, "http_request", "exit", error)
            raise
        # HTTP errors are returned responses; worker records retain their exception.
        record(index, "http_request", "exit")
        return reply

    async def run():
        try:
            replies.extend(await asyncio.gather(*(observed_post(index, app) for index, app in enumerate(apps))))
            codes = {reply.json()["error"]["code"] for reply in replies}
            assert all(reply.status_code == 409 for reply in replies), [
                (reply.status_code, reply.json()["error"]["code"]) for reply in replies]
            assert "recommendation_interrupted" in codes
            assert codes <= {"recommendation_interrupted", "recommendation_in_progress"}
            for app in apps:
                assert (await post(app, command)).json()["error"]["code"] == "recommendation_interrupted"
                assert not app.backend._executions
        finally:
            await asyncio.gather(*(app.backend.aclose() for app in apps))
    try:
        asyncio.run(run())
        assert state(application.backend, command) == dict(status="failed", failure_code="execution_interrupted",
                                                          execution_owner=original_owner)
        with application.backend._connect() as c:
            assert c.execute("SELECT count(*) AS n FROM request_items").fetchone()["n"] == 0
        assert_unlocked(application.backend, command)
    except AssertionError as failure:
        failure.add_note(repr({"replies": [(reply.status_code, safe_code(reply.json().get("error", {}).get("code")))
                                         for reply in replies], "stages": stages,
                               "observer_overhead_included": True}))
        raise


def test_orphan_retry_with_held_lease_serializes_competitors_before_terminal_reconciliation(recovery, monkeypatch):
    """Controlled service/lease ordering; not a reproduction of the HTTP 2s deadline."""
    application, command = recovery
    execution = RecommendationExecution(application.backend, command)
    execution.admit()
    execution.close()
    original_owner = state(application.backend, command)["execution_owner"]
    apps = [build_demo_application(PostgresDemoBackend(application.backend.database_url, r06_enabled=False))
            for _ in range(4)]
    winner = apps[0].backend
    entered, release = Event(), Event()
    original_admit = winner._admit

    def gated_admit(*args, **kwargs):
        winner._execution(command.request_id).assert_held()
        entered.set()
        assert release.wait(10), "orphan retry admission was not released"
        return original_admit(*args, **kwargs)

    async def unexpected_execution(*args, **kwargs):
        pytest.fail("orphan retry reached ranking or result persistence")

    monkeypatch.setattr(winner, "_admit", gated_admit)
    for app in apps:
        monkeypatch.setattr(app.backend, "rank", unexpected_execution)
        monkeypatch.setattr(app.backend, "save", unexpected_execution)

    async def competitor(app):
        with pytest.raises(IdempotencyInProgress):
            await app.recommend.execute(command)
        assert not app.backend._executions

    async def run():
        task = asyncio.create_task(apps[0].recommend.execute(command))
        tasks = [task]
        try:
            await started(entered)
            assert not task.done()
            assert winner._execution(command.request_id) is not None
            assert state(application.backend, command) == dict(status="accepted", failure_code=None,
                                                              execution_owner=original_owner)
            tasks.extend(asyncio.create_task(competitor(app)) for app in apps[1:])
            async with asyncio.timeout(5):
                await asyncio.gather(*tasks[1:])
            assert not task.done()
            assert state(application.backend, command)["status"] == "accepted"
            release.set()
            async with asyncio.timeout(5):
                with pytest.raises(ManagementError) as failure:
                    await task
            assert failure.value.status_code == 409
            assert failure.value.code == "recommendation_interrupted"
            # Remove only the test gate before terminal reads.
            monkeypatch.setattr(winner, "_admit", original_admit)
            for app in apps:
                with pytest.raises(ManagementError) as terminal:
                    await app.recommend.execute(command)
                assert terminal.value.status_code == 409
                assert terminal.value.code == "recommendation_interrupted"
                assert not app.backend._executions
        finally:
            release.set()
            try:
                async with asyncio.timeout(5):
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await asyncio.gather(*(app.backend.aclose() for app in apps))

    asyncio.run(run())
    assert state(application.backend, command) == dict(status="failed", failure_code="execution_interrupted",
                                                      execution_owner=original_owner)
    with application.backend._connect() as c:
        assert c.execute("SELECT count(*) AS n FROM request_items").fetchone()["n"] == 0
    assert_unlocked(application.backend, command)
