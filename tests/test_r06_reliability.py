"""Independent percentile oracle and actual synthetic-package process restart."""

from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest

from scripts.r06_service_lab import R06ServiceLab
from scripts.verify_r06_reliability import percentiles, summarize
from test_r06_bundle import _build


def test_latency_includes_failures_and_empty_success_is_unknown():
    records = [dict(status_code=200, elapsed_ms=10), dict(status_code=504, elapsed_ms=2000),
               dict(status_code=None, elapsed_ms=3000)]
    report = summarize(records, 4)
    assert report['successful'] == 1 and report['failures'] == 2
    assert report['success_rate'] == 1/3
    assert report['completed_requests_per_second'] == .75
    assert report['successful_requests_per_second'] == .25
    assert report['all_requests_latency']['p95_ms'] == 3000
    assert report['successful_latency']['p95_ms'] == 10
    assert summarize(records[1:], 4)['successful_latency']['p50_ms'] is None


def test_nearest_rank_percentiles_not_interpolated_or_rounded_down():
    assert percentiles([]) == dict(p50_ms=None, p95_ms=None, p99_ms=None)
    assert percentiles(list(range(1, 21))) == dict(p50_ms=10, p95_ms=19, p99_ms=20)
    with pytest.raises(ValueError): summarize([],0)
    with pytest.raises(ValueError): summarize([dict(status_code=200,elapsed_ms=float('nan'))],1)


def test_real_process_restart_keeps_same_schema_session_and_exact_result(isolated_database, tmp_path):
    root, target, digest = _build(tmp_path)
    output = Path(__file__).resolve().parents[1] / 'artifacts' / 'test-reliability' / uuid4().hex
    lab = R06ServiceLab(output, isolated_database, root, UUID(target.name), digest, 'stdlib')
    with lab:
        with httpx.Client(base_url=lab.ready['url'], timeout=10, trust_env=False) as client:
            session = client.post('/api/v1/sessions', json={'profile_id': 'sample'}).json()
            key = str(uuid4())
            body = dict(session_id=session['session_id'], expected_history_version=0, strategy='dense', k=2)
            headers = {'X-Session-Token': session['access_token'], 'Idempotency-Key': key}
            original = client.post('/api/v1/recommendations', json=body, headers=headers)
            assert original.status_code == 200, original.text
            pid = lab.ready['pid']
        lab.stop()
        lab.start()
        assert lab.ready['pid'] != pid
        with httpx.Client(base_url=lab.ready['url'], timeout=10, trust_env=False) as client:
            replay = client.post('/api/v1/recommendations', json=body, headers=headers)
            assert replay.status_code == 200 and replay.json() == original.json()
            new = client.post('/api/v1/recommendations', json=body,
                             headers={**headers, 'Idempotency-Key': str(uuid4())})
            assert new.status_code == 200 and new.json()['items'] == original.json()['items']
            denied = client.post('/api/v1/recommendations', json=body,
                                headers={**headers, 'X-Session-Token': 'wrong'})
            assert denied.status_code == 401
        with psycopg.connect(lab.isolated_url) as connection:
            assert connection.execute('SELECT count(*) FROM recommendation_requests').fetchone()[0] == 2
    assert not lab.created
    with psycopg.connect(isolated_database) as connection:
        assert not connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (lab.schema,)).fetchone()


def test_lab_rejects_remote_or_existing_output_before_schema_mutation(tmp_path):
    with pytest.raises(ValueError):
        R06ServiceLab(tmp_path / 'invalid', 'host=remote', tmp_path, uuid4(), 'a'*64)
