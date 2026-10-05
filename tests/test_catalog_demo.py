"""Isolated content-demo TCP acceptance; synthetic data, not R06 research."""
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from uuid import uuid4

import httpx
import psycopg
import pytest

from scripts.run_catalog_demo import main, run
from scripts.run_r06_demo import validate_isolation

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('port', [8000, -1, 65536, True])
def test_port_guard_before_output_or_database(port):
    output = ROOT / 'artifacts/catalog-demo-tests' / uuid4().hex
    with pytest.raises(ValueError):
        validate_isolation(output, 'host=localhost dbname=test', port)
    assert not output.exists()


@pytest.mark.parametrize('database', ['host=example.com', 'host=localhost hostaddr=203.0.113.1', 'dbname=test'])
def test_database_guard_before_output(database):
    output = ROOT / 'artifacts/catalog-demo-tests' / uuid4().hex
    with pytest.raises(ValueError):
        validate_isolation(output, database, 0)
    assert not output.exists()


def test_missing_configuration_is_sanitized(monkeypatch, capsys):
    monkeypatch.delenv('EVOREC_DATABASE_URL', raising=False)
    assert main([str(ROOT / 'artifacts/catalog-demo-tests' / uuid4().hex)]) == 1
    assert json.loads(capsys.readouterr().out) == {'status':'failed', 'code':'database_not_configured'}


def test_direct_run_rejects_inherited_business_admin(monkeypatch):
    monkeypatch.setenv('EVOREC_ADMIN_TOKEN', 'must-not-use-business-admin')
    output = ROOT / 'artifacts/catalog-demo-tests' / uuid4().hex
    with pytest.raises(ValueError):
        run(output, 'host=localhost')
    assert not output.exists()


def test_output_guard(tmp_path):
    with pytest.raises(ValueError):
        validate_isolation(ROOT / 'tmp' / uuid4().hex, 'host=localhost', 0)
    with pytest.raises(ValueError):
        validate_isolation(ROOT / 'artifacts', 'host=localhost', 0)
    existing=ROOT/'artifacts/catalog-demo-tests'/uuid4().hex
    existing.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        validate_isolation(existing, 'host=localhost', 0)


def test_occupied_port_cannot_create_output_or_database(monkeypatch):
    import socket
    monkeypatch.delenv('EVOREC_ADMIN_TOKEN',raising=False)
    output=ROOT/'artifacts/catalog-demo-tests'/uuid4().hex
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1',0))
        reserved.listen()
        with pytest.raises(OSError):
            run(output,'host=localhost',port=reserved.getsockname()[1])
    assert not output.exists()


def test_worker_exit_waits_for_actual_thread(monkeypatch,tmp_path):
    import asyncio
    from threading import Event
    from types import SimpleNamespace
    import scripts.run_catalog_demo as demo
    started,release,finished=Event(),Event(),Event()
    def run_next(_):
        started.set()
        assert release.wait(5)
        finished.set()
        return False
    async def fake_serve(*_):
        assert await asyncio.to_thread(started.wait,5)
    monkeypatch.setattr(demo,'serve',fake_serve)
    application=SimpleNamespace(manager=SimpleNamespace(file_jobs=SimpleNamespace(run_next=run_next),
        builds=SimpleNamespace(run_next=lambda:False)))
    async def exercise():
        task=asyncio.create_task(demo.serve_catalog(application,None,tmp_path,{}))
        try:
            assert await asyncio.to_thread(started.wait,5)
            await asyncio.sleep(.03)
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
            await task
        assert finished.is_set()
    asyncio.run(exercise())


def test_worker_failure_stops_listener_and_propagates(monkeypatch,tmp_path):
    import asyncio
    from types import SimpleNamespace
    import scripts.run_catalog_demo as demo
    def failed(_):
        raise RuntimeError('synthetic worker failure')
    async def fake_serve(*_):
        deadline=monotonic()+5
        while not (tmp_path/'stop').exists():
            assert monotonic()<deadline
            await asyncio.sleep(.01)
    monkeypatch.setattr(demo,'serve',fake_serve)
    application=SimpleNamespace(manager=SimpleNamespace(file_jobs=SimpleNamespace(run_next=failed)))
    with pytest.raises(RuntimeError,match='synthetic worker failure'):
        asyncio.run(demo.serve_catalog(application,None,tmp_path,{}))
    assert (tmp_path/'stop').exists()


def test_setup_failure_removes_only_owned_schema(isolated_database,monkeypatch):
    import scripts.run_catalog_demo as demo
    monkeypatch.delenv('EVOREC_ADMIN_TOKEN',raising=False)
    def failed(*_,**__):
        raise ValueError('synthetic setup failure')
    monkeypatch.setattr(demo,'build_demo_application',failed)
    output=ROOT/'artifacts/catalog-demo-tests'/uuid4().hex
    with pytest.raises(ValueError,match='synthetic setup failure'):
        run(output,isolated_database)
    stopped=json.loads((output/'stopped.json').read_bytes())
    assert stopped['owned_schema_removed']
    assert not (output/'ready.json').exists() and not (output/'admin-token.txt').exists()
    with psycopg.connect(isolated_database) as connection:
        assert connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(stopped['schema'],)).fetchone() is None


def test_real_child_csv_build_publish_deactivate_rollback_and_cleanup(isolated_database):
    output = ROOT / 'artifacts/catalog-demo-tests' / uuid4().hex
    environment = {k:v for k,v in os.environ.items() if not k.startswith('EVOREC_')}
    environment.update(EVOREC_DATABASE_URL=isolated_database,
                       EVOREC_ADMIN_TOKEN='must-not-reach-isolated-demo-business-token',
                       EVOREC_R06_SERVING_ENABLED='1')
    child = subprocess.Popen([sys.executable, '-m', 'scripts.run_catalog_demo', str(output)],
        cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    ready = None
    try:
        deadline = monotonic()+30
        while not (output/'ready.json').exists():
            assert child.poll() is None, 'child exited before readiness'
            assert monotonic() < deadline, 'readiness deadline exceeded'
            sleep(.05)
        ready = json.loads((output/'ready.json').read_bytes())
        assert ready['admin_enabled'] and ready['worker_enabled'] and ready['item_count']==24
        assert ready['model_kind']=='content-baseline' and ready['ephemeral']
        assert ':8000' not in ready['url'] and ready['url'].startswith('http://127.0.0.1:')
        token = (output/'admin-token.txt').read_text()
        assert len(token)>=32 and token!=environment['EVOREC_ADMIN_TOKEN']
        assert token not in json.dumps(ready)
        with httpx.Client(base_url=ready['url'],trust_env=False,timeout=10) as client:
            from scripts.verify_catalog_demo import exercise
            checks = exercise(client,ready,token,output)
            assert checks['cold_item_recommendable_only_after_publish']
            assert checks['deactivate_and_rollback_do_not_reactivate']
            assert checks['invalid_csv_atomic_and_row_errors']
            assert checks['original_recommendation_replays_after_rollback']
    finally:
        if child.poll() is None:
            if output.is_dir():
                (output/'stop').touch()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=5)
                pytest.fail('force kill required; cleanup not proven')
        log = child.stdout.read().decode('utf-8',errors='replace')
        child.stdout.close()
    assert child.returncode==0, log
    stopped = json.loads((output/'stopped.json').read_bytes())
    assert stopped['worker_drained'] and stopped['owned_schema_removed']
    assert ready['run_id']==stopped['run_id'] and ready['schema']==stopped['schema']
    assert not (output/'admin-token.txt').exists()
    assert 'must-not-reach-isolated-demo-business-token' not in log
    with psycopg.connect(isolated_database) as connection:
        assert connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(ready['schema'],)).fetchone() is None
