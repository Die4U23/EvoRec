"""Real database execution-lock and terminal crash reconciliation, not reranking."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from threading import Event
import time
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.pq import TransactionStatus

from evorec.api.app import create_app
from evorec.bootstrap import build_demo_application
from evorec.domain.errors import ManagementError, SnapshotMismatch
from evorec.domain.models import RecommendationCommand, RecommendationResult, Strategy
from evorec.domain.recommendation import select_results
from evorec.infrastructure.postgres import PostgresDemoBackend
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


def result_for(application, context, command):
    batch = asyncio.run(application.backend.rank(context, command))
    return RecommendationResult(context.binding, command.strategy, batch.actual_strategy,
                                select_results(batch.candidates, context, command.k), batch.fallback_reason)


def test_result_write_reuses_owned_connection_and_leaves_idle_lock_until_close(recovery, monkeypatch):
    application, command = recovery
    backend = application.backend
    execution = RecommendationExecution(backend, command)
    try:
        context = execution.admit()
        result = result_for(application, context, command)
        owner = execution.connection
        pid = owner.info.backend_pid
        # Real admitted request and real writes; any new physical connection is a failure.
        with monkeypatch.context() as patch:
            def forbidden_connect():
                pytest.fail("result write opened another physical connection")
            patch.setattr(backend, "_connect", forbidden_connect)
            asyncio.run(backend.save(result))
        assert execution.connection is owner and owner.info.backend_pid == pid
        assert not owner.closed and owner.autocommit
        assert owner.info.transaction_status == TransactionStatus.IDLE
        execution.assert_held()
        assert state(backend, command)["status"] == "completed"
        # Commit does not release the session lease.
        with backend._connect() as c:
            assert not c.execute("SELECT pg_try_advisory_xact_lock(%s) AS held",
                                 (execution.lock_key,)).fetchone()["held"]
    finally:
        execution.close()
    assert owner.closed
    assert_unlocked(backend, command)
    first, second = asyncio.run(post(application, command)), asyncio.run(post(application, command))
    assert first.status_code == second.status_code == 200 and first.json() == second.json()


def test_result_transaction_failure_rolls_back_all_items_without_closing_lease(recovery):
    application, command = recovery
    backend = application.backend
    execution = RecommendationExecution(backend, command)
    try:
        context = execution.admit()
        result = result_for(application, context, command)
        assert result.items
        with backend._connect() as c:
            c.execute("CREATE FUNCTION reject_result_commit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                      "BEGIN IF NEW.status='completed' THEN RAISE check_violation; END IF; RETURN NEW; END $$")
            c.execute("CREATE TRIGGER reject_result_commit BEFORE UPDATE ON recommendation_requests "
                      "FOR EACH ROW EXECUTE FUNCTION reject_result_commit()")
        with pytest.raises(psycopg.errors.CheckViolation):
            asyncio.run(backend.save(result))
        assert not execution.connection.closed
        assert execution.connection.info.transaction_status == TransactionStatus.IDLE
        execution.assert_held()
        assert state(backend, command)["status"] == "accepted"
        with backend._connect() as c:
            assert c.execute("SELECT count(*) AS n FROM request_items WHERE request_id=%s",
                             (command.request_id,)).fetchone()["n"] == 0
            c.execute("DROP TRIGGER reject_result_commit ON recommendation_requests")
            c.execute("DROP FUNCTION reject_result_commit()")
        asyncio.run(backend.save(result))
        assert state(backend, command)["status"] == "completed"
        assert execution.connection.info.transaction_status == TransactionStatus.IDLE
        execution.assert_held()
    finally:
        execution.close()
    assert_unlocked(backend, command)


def test_result_borrow_refuses_outer_transaction_instead_of_returning_uncommitted_success(recovery):
    application, command = recovery
    backend = application.backend
    execution = RecommendationExecution(backend, command)
    try:
        context = execution.admit()
        result = result_for(application, context, command)
        execution.connection.execute("BEGIN")
        with pytest.raises(ManagementError) as failure:
            asyncio.run(backend.save(result))
        assert failure.value.code == "recommendation_execution_lost"
        with backend._connect() as c:
            assert c.execute("SELECT count(*) AS n FROM request_items WHERE request_id=%s",
                             (command.request_id,)).fetchone()["n"] == 0
        assert state(backend, command)["status"] == "accepted"
        execution.connection.rollback()
        asyncio.run(backend.save(result))
        assert state(backend, command)["status"] == "completed"
        execution.assert_held()
    finally:
        execution.close()
    assert_unlocked(backend, command)


def test_concurrent_borrowers_cannot_share_transaction_or_rollback_each_other(recovery):
    application, command = recovery
    backend = application.backend
    execution = RecommendationExecution(backend, command)
    entered, release, attempting, second_entered = Event(), Event(), Event(), Event()
    try:
        execution.admit()
        with backend._connect() as c:
            c.execute("CREATE TABLE result_connection_probe (value integer PRIMARY KEY)")

        def first():
            with execution.result_transaction() as c:
                c.execute("INSERT INTO result_connection_probe VALUES (1)")
                entered.set()
                assert release.wait(10)
                raise RuntimeError("intentional rollback")

        def second():
            attempting.set()
            with execution.result_transaction() as c:
                second_entered.set()
                c.execute("INSERT INTO result_connection_probe VALUES (2)")

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first)
            try:
                assert entered.wait(5)
                b = pool.submit(second)
                assert attempting.wait(5)
                assert not second_entered.wait(.2)
            finally:
                release.set()
            with pytest.raises(RuntimeError, match="intentional rollback"):
                a.result(timeout=5)
            b.result(timeout=5)
        assert second_entered.is_set()
        with backend._connect() as c:
            assert c.execute("SELECT value FROM result_connection_probe ORDER BY value").fetchall() == [{"value": 2}]
        assert execution.connection.info.transaction_status == TransactionStatus.IDLE
        execution.assert_held()
    finally:
        release.set()
        execution.close()
    assert_unlocked(backend, command)


def test_cancel_while_real_result_transaction_waits_drains_before_lease_close(recovery, monkeypatch):
    application, command = recovery
    backend = application.backend
    entered, release = Event(), Event()
    original_rank = backend.rank

    async def paused_rank(context, cmd):
        def pause():
            entered.set()
            assert release.wait(10)
        await backend.r06_queue.run(pause)
        return await original_rank(context, cmd)

    monkeypatch.setattr(backend, "rank", paused_rank)

    async def run():
        task = asyncio.create_task(application.recommend.execute(command))
        blocker = backend._connect()
        observer = backend._connect()
        observer.autocommit = True
        try:
            await started(entered)
            execution = backend._execution(command.request_id)
            owner = execution.connection
            blocker.execute("SELECT request_id FROM recommendation_requests WHERE request_id=%s FOR UPDATE",
                            (command.request_id,)).fetchone()
            release.set()
            async with asyncio.timeout(10):
                while not observer.execute("SELECT %s = ANY(pg_blocking_pids(%s)) AS waiting",
                                           (blocker.info.backend_pid, owner.info.backend_pid)).fetchone()["waiting"]:
                    await asyncio.sleep(.02)
            # The owned physical session, not a new result connection, is blocked.
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(.01)
                assert not task.done() and not owner.closed
            reply = await post(application, command)
            assert reply.json()["error"]["code"] == "recommendation_in_progress"
            blocker.rollback()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert owner.closed and not backend._executions
            assert state(backend, command)["status"] == "completed"
            first, second = await post(application, command), await post(application, command)
            assert first.status_code == second.status_code == 200 and first.json() == second.json()
            assert_unlocked(backend, command)
        finally:
            release.set()
            blocker.close()
            observer.close()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


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


def test_concurrent_orphan_retries_share_one_terminal_outcome_without_execution(recovery):
    application, command = recovery
    execution = RecommendationExecution(application.backend, command)
    execution.admit()
    execution.close()
    original_owner = state(application.backend, command)["execution_owner"]
    apps = [build_demo_application(PostgresDemoBackend(application.backend.database_url, r06_enabled=False))
            for _ in range(4)]
    async def run():
        replies = await asyncio.gather(*(post(app, command) for app in apps))
        codes = {reply.json()["error"]["code"] for reply in replies}
        assert all(reply.status_code == 409 for reply in replies), [
            (reply.status_code, reply.json()["error"]["code"]) for reply in replies
        ]
        assert "recommendation_interrupted" in codes
        assert codes <= {"recommendation_interrupted", "recommendation_in_progress"}
        for app in apps:
            assert (await post(app, command)).json()["error"]["code"] == "recommendation_interrupted"
            assert not app.backend._executions
    asyncio.run(run())
    assert state(application.backend, command) == dict(status="failed", failure_code="execution_interrupted",
                                                      execution_owner=original_owner)
    with application.backend._connect() as c:
        assert c.execute("SELECT count(*) AS n FROM request_items").fetchone()["n"] == 0
    assert_unlocked(application.backend, command)
