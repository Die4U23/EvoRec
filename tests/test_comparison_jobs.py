"""Real PostgreSQL queue, cancellation, fencing and independent worker recovery checks."""

import asyncio
import subprocess
import sys
import threading
import time
from dataclasses import replace
from uuid import uuid4

import httpx
import psycopg
import pytest

from evorec.api.app import create_app
from evorec.application.compare import ComparisonCommand
from evorec.bootstrap import build_demo_application
from evorec.domain.errors import IdempotencyConflict, ManagementError
from evorec.domain.models import Strategy
from evorec.infrastructure.comparison_job import ComparisonLeaseLost
from test_strategy_comparison import published_application


async def queued_job(application, k=2):
    session = await application.backend.create_session()
    command = ComparisonCommand(uuid4(), session.snapshot.session_id, session.access_token, 0,
                                (Strategy.POPULAR, Strategy.DENSE, Strategy.ADAPTIVE), k)
    return command, await application.comparison_jobs.enqueue(command)


def count_results(database_url):
    with psycopg.connect(database_url) as connection:
        return connection.execute('SELECT count(*) FROM strategy_comparisons').fetchone()[0]


def test_comparison_job_api_freezes_snapshot_replays_and_checks_ownership(
    isolated_database, monkeypatch, tmp_path,
):
    application, build = published_application(monkeypatch, tmp_path)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=application)),
                                     base_url='http://test') as client:
            session = (await client.post('/api/v1/sessions')).json()
            key = str(uuid4())
            headers = {'X-Session-Token': session['access_token'], 'Idempotency-Key': key}
            body = {'session_id': session['session_id'], 'expected_history_version': 0,
                    'strategies': ['popular', 'dense', 'adaptive'], 'k': 2}
            root = '/api/v1/strategy-comparison-jobs'
            query = f'{root}/{key}?session_id={session["session_id"]}'
            assert (await client.post(root, json=body)).status_code == 401
            assert (await client.post(root, json=body, headers={**headers, 'X-Session-Token': 'wrong'})).status_code == 401
            queued = await client.post(root, json=body, headers=headers)
            assert queued.status_code == 202, queued.text
            original = queued.json()
            assert original['status'] == 'queued' and original['attempts'] == 0
            assert original['bundle_id'] == str(build['bundle_id'])
            assert (await client.post(root, json=body, headers=headers)).json() == original
            assert (await client.post(root, json={**body, 'k': 1}, headers=headers)).status_code == 409
            assert (await client.post('/api/v1/strategy-comparisons', json=body, headers=headers)).status_code == 409
            assert (await client.get(query)).status_code == 401
            other = (await client.post('/api/v1/sessions')).json()
            assert (await client.get(f'{root}/{key}?session_id={other["session_id"]}', headers={
                'X-Session-Token': other['access_token'],
            })).status_code == 404
            assert (await client.post(f'{root}/{key}/cancel?session_id={other["session_id"]}', headers={
                'X-Session-Token': other['access_token'],
            })).status_code == 404
            await client.post(f'/api/v1/sessions/{session["session_id"]}/reset', headers=headers)
            with psycopg.connect(isolated_database) as connection:
                connection.execute('UPDATE items SET is_active = false')
                connection.execute('UPDATE catalog_control SET admission_open = false')
            # Independent worker application loads the frozen registered bundle, not current admission.
            restarted = build_demo_application()
            assert await restarted.comparison_jobs.run_next()
            completed = (await client.get(query, headers=headers)).json()
            assert completed['status'] == 'completed' and completed['completed_strategies'] == 3
            assert completed['comparison_id'] == key
            saved = (await client.get(f'/api/v1/strategy-comparisons/{key}?session_id={session["session_id"]}',
                                      headers=headers)).json()
            assert saved['history_version'] == 0 and saved['session_epoch'] == 0
            assert saved['snapshot_at'] == original['snapshot_at']
            assert saved['input_snapshot']['eligible_items'] == ['alpha', 'beta']
            assert [r['actual_strategy'] for r in saved['strategies']] == ['popular', 'dense', 'popular']
            assert (await client.post(root, json=body, headers=headers)).json() == completed
            cancel = await client.post(f'{root}/{key}/cancel?session_id={session["session_id"]}', headers=headers)
            assert cancel.json()['status'] == 'completed'  # Cannot unpublish a completed result by cancelling.
    asyncio.run(exercise())
    with psycopg.connect(isolated_database) as connection:
        frozen = connection.execute('SELECT frozen_input FROM strategy_comparison_jobs').fetchone()[0]
        assert 'token' not in str(frozen)
        assert connection.execute('SELECT count(*) FROM recommendation_requests').fetchone()[0] == 0
    assert count_results(isolated_database) == 1


@pytest.mark.parametrize('phase', ['queued', 'running'])
def test_cancellation_never_persists_partial_results(isolated_database, monkeypatch, tmp_path, phase):
    application, _ = published_application(monkeypatch, tmp_path)
    service = application.comparison_jobs
    started, release = threading.Event(), threading.Event()
    real_rank = application.backend.rank
    calls = []

    async def paused_rank(context, command):
        calls.append(command.strategy)
        started.set()
        assert await asyncio.to_thread(release.wait, 10), 'test did not release ranking'
        return await real_rank(context, command)
    monkeypatch.setattr(application.backend, 'rank', paused_rank)

    async def exercise():
        command, _ = await queued_job(application)
        worker = None
        try:
            if phase == 'running':
                worker = asyncio.create_task(service.run_next())
                assert await asyncio.to_thread(started.wait, 10)
            state = await service.cancel(command.comparison_id, command.session_id, command.session_token)
            assert state['status'] == ('cancelled' if phase == 'queued' else 'cancelling')
            assert count_results(isolated_database) == 0
            if worker:
                # Cancellation request does not pretend the still-paused path has terminated.
                assert not worker.done()
                assert not await build_demo_application().comparison_jobs.run_next()
                release.set()
                assert await worker
            else:
                assert not await service.run_next()
            final = await service.get(command.comparison_id, command.session_id, command.session_token)
            assert final['status'] == 'cancelled' and final['comparison_id'] is None
            assert calls == ([Strategy.POPULAR] if phase == 'running' else [])
            assert (await service.enqueue(command))['status'] == 'cancelled'
        finally:
            release.set()
            if worker is not None:
                await worker
    asyncio.run(exercise())
    assert count_results(isolated_database) == 0


@pytest.mark.parametrize('failure', ['rank', 'bundle_missing', 'timeout', 'lost_commit_response'])
def test_job_failure_boundaries(isolated_database, monkeypatch, tmp_path, failure):
    application, _ = published_application(monkeypatch, tmp_path)
    service = application.comparison_jobs
    real_finish = service._finish

    async def fail_rank(context, command):
        if failure == 'timeout':
            raise TimeoutError()
        raise RuntimeError('private failure text must not be stored')

    def lost_finish(*args, **kwargs):
        real_finish(*args, **kwargs)
        raise psycopg.OperationalError('simulated connection lost after durable commit')

    async def exercise():
        command, _ = await queued_job(application)
        if failure in ('rank', 'timeout'):
            monkeypatch.setattr(application.backend, 'rank', fail_rank)
        elif failure == 'bundle_missing':
            # Missing immutable artifact must not silently switch dense to popular.
            runtime = application.backend.runtimes[str((await service.get(
                command.comparison_id, command.session_id, command.session_token))['bundle_id'])]
            (application.manager.managed_root / runtime.bundle_id / 'manifest.json').unlink()
        else:
            monkeypatch.setattr(service, '_finish', lost_finish)
        if failure == 'lost_commit_response':
            with pytest.raises(psycopg.OperationalError):
                await service.run_next()
            monkeypatch.setattr(service, '_finish', real_finish)
        else:
            assert await service.run_next()
        state = await service.get(command.comparison_id, command.session_id, command.session_token)
        assert state['status'] == ('completed' if failure == 'lost_commit_response' else 'failed')
        assert 'private' not in str(state)
        assert not await service.run_next()
        assert (await service.enqueue(command))['status'] == state['status']
    asyncio.run(exercise())
    assert count_results(isolated_database) == (1 if failure == 'lost_commit_response' else 0)


def test_expired_attempt_is_fenced_and_live_worker_lock_prevents_reclaim(
    isolated_database, monkeypatch, tmp_path,
):
    application, _ = published_application(monkeypatch, tmp_path)
    service = application.comparison_jobs

    async def exercise():
        command, _ = await queued_job(application)
        lock, stale = service._claim()
        try:
            with psycopg.connect(isolated_database) as connection:
                connection.execute("UPDATE strategy_comparison_jobs SET lease_until = now() - interval '1 second'")
            assert not await build_demo_application().comparison_jobs.run_next()
            with pytest.raises(ComparisonLeaseLost):
                service._touch(stale, 1)
            with pytest.raises(ComparisonLeaseLost):
                service._finish(stale, error_code='stale')
        finally:
            lock.close()
        assert await build_demo_application().comparison_jobs.run_next()
        assert (await service.get(command.comparison_id, command.session_id, command.session_token))['attempts'] == 2
        with pytest.raises(ComparisonLeaseLost):
            service._finish(stale, error_code='late_stale_completion')
    asyncio.run(exercise())
    assert count_results(isolated_database) == 1


def test_queue_limits_and_sync_key_collision(isolated_database, monkeypatch, tmp_path):
    application, _ = published_application(monkeypatch, tmp_path)

    async def exercise():
        command, _ = await queued_job(application)
        for _ in range(9):
            await application.comparison_jobs.enqueue(replace(command, comparison_id=uuid4()))
        with pytest.raises(ManagementError) as caught:
            await application.comparison_jobs.enqueue(replace(command, comparison_id=uuid4()))
        assert caught.value.code == 'comparison_queue_full'
        assert caught.value.status_code == 429
        # A successful synchronous save cannot be stolen by a background job.
        saved_command = replace(command, comparison_id=uuid4())
        await application.compare.save(saved_command)
        with pytest.raises(IdempotencyConflict):
            await application.comparison_jobs.enqueue(saved_command)
    asyncio.run(exercise())


@pytest.mark.parametrize('cancelled', [False, True])
def test_expired_retry_limit_and_cancel_recovery(isolated_database, monkeypatch, tmp_path, cancelled):
    application, _ = published_application(monkeypatch, tmp_path)
    service = application.comparison_jobs

    async def exercise():
        command, _ = await queued_job(application)
        lock, _ = service._claim()
        lock.close()  # Simulate no live holder remaining after a worker crash.
        with psycopg.connect(isolated_database) as connection:
            connection.execute("UPDATE strategy_comparison_jobs SET attempts = 3, lease_until = now() - interval '1 second'")
        if cancelled:
            assert (await service.cancel(command.comparison_id, command.session_id, command.session_token))['status'] == 'cancelling'
        assert await service.run_next()
        state = await service.get(command.comparison_id, command.session_id, command.session_token)
        assert state['status'] == ('cancelled' if cancelled else 'failed')
        assert state['error_code'] == (None if cancelled else 'attempts_exhausted')
        assert state['attempts'] == 3
        assert not await service.run_next()
    asyncio.run(exercise())
    assert count_results(isolated_database) == 0


def test_concurrent_enqueue_and_lost_response_retain_one_job(isolated_database, monkeypatch, tmp_path):
    application, _ = published_application(monkeypatch, tmp_path)
    service = application.comparison_jobs
    real_capture = application.compare.capture
    real_enqueue = service._enqueue
    calls = 0
    gate = asyncio.Event()

    async def capture(command):
        nonlocal calls
        snapshot = await real_capture(command)
        calls += 1
        if calls == 2:
            gate.set()
        await gate.wait()
        return snapshot
    monkeypatch.setattr(application.compare, 'capture', capture)

    async def exercise():
        created = await application.backend.create_session()
        command = ComparisonCommand(uuid4(), created.snapshot.session_id, created.access_token, 0,
                                    (Strategy.POPULAR, Strategy.DENSE), 2)
        first, second = await asyncio.gather(service.enqueue(command), service.enqueue(command))
        assert calls == 2 and first == second
        monkeypatch.setattr(application.compare, 'capture', real_capture)
        retry = replace(command, comparison_id=uuid4())
        def lost_response(*args):
            real_enqueue(*args)
            raise psycopg.OperationalError('simulated lost enqueue response')
        monkeypatch.setattr(service, '_enqueue', lost_response)
        with pytest.raises(psycopg.OperationalError):
            await service.enqueue(retry)
        monkeypatch.setattr(service, '_enqueue', real_enqueue)
        assert (await service.enqueue(retry))['status'] == 'queued'
        with psycopg.connect(isolated_database) as connection:
            assert connection.execute('SELECT count(*) FROM strategy_comparison_jobs').fetchone()[0] == 2
    asyncio.run(exercise())


def test_worker_coroutine_shutdown_keeps_lock_until_underlying_rank_exits(
    isolated_database, monkeypatch, tmp_path,
):
    application, _ = published_application(monkeypatch, tmp_path)
    service = application.comparison_jobs
    started, release = threading.Event(), threading.Event()
    real_rank = application.backend.rank

    async def paused_rank(context, command):
        started.set()
        assert await asyncio.to_thread(release.wait, 10)
        return await real_rank(context, command)
    monkeypatch.setattr(application.backend, 'rank', paused_rank)

    async def exercise():
        command, _ = await queued_job(application)
        task = asyncio.create_task(service.run_next())
        try:
            assert await asyncio.to_thread(started.wait, 10)
            task.cancel()
            await asyncio.sleep(0.1)
            assert not task.done(), 'shutdown must wait for the underlying ranking thread'
            with psycopg.connect(isolated_database) as connection:
                connection.execute("UPDATE strategy_comparison_jobs SET lease_until = now() - interval '1 second'")
            assert not await build_demo_application().comparison_jobs.run_next()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert count_results(isolated_database) == 0
        assert await build_demo_application().comparison_jobs.run_next()
        assert (await service.get(command.comparison_id, command.session_id, command.session_token))['status'] == 'completed'
    asyncio.run(exercise())


def test_heartbeat_keeps_attempt_alive_and_progress_is_durable(isolated_database, monkeypatch, tmp_path):
    application, _ = published_application(monkeypatch, tmp_path)
    service = application.comparison_jobs
    service.LEASE_SECONDS = 2
    service.HEARTBEAT_SECONDS = 0.1
    real_rank = application.backend.rank
    first_done, release = threading.Event(), threading.Event()

    async def paused_second_rank(context, command):
        if command.strategy == Strategy.DENSE:
            first_done.set()
            assert await asyncio.to_thread(release.wait, 10)
        return await real_rank(context, command)
    monkeypatch.setattr(application.backend, 'rank', paused_second_rank)

    async def exercise():
        command, _ = await queued_job(application)
        task = asyncio.create_task(service.run_next())
        try:
            assert await asyncio.to_thread(first_done.wait, 10)
            await asyncio.sleep(2.1)  # Beyond original lease; heartbeat must renew it.
            state = await service.get(command.comparison_id, command.session_id, command.session_token)
            assert state['status'] == 'running' and state['completed_strategies'] == 1
            with psycopg.connect(isolated_database) as connection:
                assert connection.execute('SELECT lease_until > clock_timestamp() FROM strategy_comparison_jobs').fetchone()[0]
            assert not await build_demo_application().comparison_jobs.run_next()
        finally:
            release.set()
            assert await task
        assert (await service.get(command.comparison_id, command.session_id, command.session_token))['status'] == 'completed'
    asyncio.run(exercise())


def test_real_worker_kill_and_restart_reuses_frozen_input(isolated_database, monkeypatch, tmp_path):
    application, _ = published_application(monkeypatch, tmp_path)
    command, original = asyncio.run(queued_job(application))
    rank_marker = tmp_path / 'comparison-ranking-started'
    child_source = (
        'import asyncio, time\n'
        'from pathlib import Path\n'
        'from evorec.bootstrap import build_demo_application\n'
        'app = build_demo_application()\n'
        'async def paused_rank(context, command):\n'
        f'    Path({str(rank_marker)!r}).touch()\n'
        '    time.sleep(120)\n'
        'app.backend.rank = paused_rank\n'
        'asyncio.run(app.comparison_jobs.run_next())\n'
    )
    worker = subprocess.Popen([sys.executable, '-c', child_source],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = asyncio.run(application.comparison_jobs.get(command.comparison_id, command.session_id,
                                                                command.session_token))
            if state['status'] == 'running' and rank_marker.exists():
                break
            if worker.poll() is not None:
                pytest.fail('worker exited early: ' + str(worker.communicate()))
            time.sleep(0.05)
        else:
            pytest.fail('worker did not claim the queued comparison')
        second = subprocess.run([sys.executable, '-m', 'scripts.comparison_worker', '--once'],
                                capture_output=True, text=True, timeout=20)
        assert second.returncode == 0, second.stdout + second.stderr
        assert count_results(isolated_database) == 0
        worker.kill()
        worker.communicate(timeout=5)
        # Explicitly expire the killed lease rather than waiting 30 seconds in a regression test.
        with psycopg.connect(isolated_database) as connection:
            connection.execute("UPDATE strategy_comparison_jobs SET lease_until = now() - interval '1 second'")
        restarted = subprocess.run([sys.executable, '-m', 'scripts.comparison_worker', '--once'],
                                   capture_output=True, text=True, timeout=20)
        assert restarted.returncode == 0, restarted.stdout + restarted.stderr
        state = asyncio.run(application.comparison_jobs.get(command.comparison_id, command.session_id,
                                                            command.session_token))
        assert state['status'] == 'completed' and state['attempts'] == 2
        assert state['snapshot_at'] == original['snapshot_at']
        assert count_results(isolated_database) == 1
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=5)
