"""Fault controls for strict evidence; synthetic tests are not full-model SLA."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest

from scripts import check_r06_stable_recovery as gates
from scripts import profile_r06_tcp as profiler
from scripts import verify_r06_reliability as verifier
from scripts.check_r06_trace_abba import BUNDLE, MANIFEST, MODEL
from scripts.r06_service_lab import R06ServiceLab
from test_r06_bundle import _build


def bindings():
    key = str(uuid4())
    ready = dict(bundle_id=BUNDLE, model_version=MODEL, manifest_sha256=MANIFEST, url='http://127.0.0.1:1')
    session = dict(session_id=str(uuid4()), epoch=0, history_version=3, history=['seen'], access_token='PRIVATE-TOKEN')
    body = dict(request_id=key, session_id=session['session_id'], session_epoch=0, history_version=3,
                bundle_id=BUNDLE, exclusion_version=1, requested_strategy='dense', actual_strategy='dense',
                fallback_reason=None, model_version=MODEL, captured_at_ms=100,
                items=[dict(item_id=str(i), score=.5, source='r06-a-frozen-s17') for i in range(10)])
    row = {k: v for k, v in body.items() if k not in ('items', 'model_version', 'captured_at_ms')}
    row.update(status='completed', requested_k=10, execution_owner=uuid4(), lease_released=True,
               model_snapshot=dict(timestamp_ms=100, manifest_sha256=MANIFEST, model_version=MODEL, full_seen=['seen']))
    items = [dict(position=i + 1, approved_member=True, **item) for i, item in enumerate(body['items'])]
    return key, ready, session, body, row, items


@pytest.mark.parametrize('defect', [None, 'key', 'epoch', 'version', 'bool_epoch', 'model', 'bundle',
                                   'requested', 'duplicate', 'seen', 'nan', 'inf', 'bool_score', 'missing', 'text'])
def test_receipt_requires_exact_caller_identity_and_legal_items(defect):
    key, ready, session, body, _, _ = bindings()
    if defect == 'key': body['request_id'] = str(uuid4())
    elif defect == 'epoch': body['session_epoch'] = 1
    elif defect == 'version': body['history_version'] = 4
    elif defect == 'bool_epoch': body['session_epoch'] = False
    elif defect == 'model': body['model_version'] = 'other'
    elif defect == 'bundle': body['bundle_id'] = str(uuid4())
    elif defect == 'requested': body['requested_strategy'] = 'popular'
    elif defect == 'duplicate': body['items'][1] = deepcopy(body['items'][0])
    elif defect == 'seen': body['items'][0]['item_id'] = 'seen'
    elif defect in ('nan', 'inf'): body['items'][0]['score'] = float(defect)
    elif defect == 'bool_score': body['items'][0]['score'] = True
    elif defect == 'missing': del body['captured_at_ms']
    elif defect == 'text': body = 'PRIVATE-RESPONSE'
    assert gates.strict_legal(body, ready, session, key) is (defect is None)


@pytest.mark.parametrize('defect', [None, 'accepted', 'missing', 'binding', 'manifest', 'timestamp', 'owner',
                                   'lease', 'count', 'position', 'score', 'source', 'full_seen', 'member'])
def test_durable_receipt_must_be_real_terminal_and_exact_order(defect):
    key, ready, session, body, row, items = bindings()
    if defect == 'accepted': row['status'] = 'accepted'
    elif defect == 'missing': row = None
    elif defect == 'binding': row['history_version'] += 1
    elif defect == 'manifest': row['model_snapshot']['manifest_sha256'] = 'f' * 64
    elif defect == 'timestamp': row['model_snapshot']['timestamp_ms'] += 1
    elif defect == 'owner': row['execution_owner'] = None
    elif defect == 'lease': row['lease_released'] = False
    elif defect == 'count': items.pop()
    elif defect == 'position': items[0]['position'] = 2
    elif defect == 'score': items[0]['score'] = .6
    elif defect == 'source': items[0]['source'] = 'popular'
    elif defect == 'full_seen': row['model_snapshot']['full_seen'].append('0')
    elif defect == 'member': items[0]['approved_member'] = False
    assert gates.durable_matches(body, row, items, ready, session, key) is (defect is None)


@pytest.mark.parametrize('outcome', ['completed', 'execution_interrupted', 'execution_failed'])
def test_terminal_audit_uses_the_public_error_contract_not_internal_failure_code(monkeypatch, outcome):
    key, ready, session, body, row, items = bindings()
    if outcome != 'completed':
        row.update(status='failed', failure_code=outcome)
        items = []
        body = dict(error=dict(request_id=key, retryable=False, code='recommendation_interrupted'
                    if outcome == 'execution_interrupted' else 'recommendation_idempotency_conflict'))
    lab = SimpleNamespace(ready=ready, isolated_url='PRIVATE-DB', observations={})
    monkeypatch.setattr(verifier, 'read_ledger', lambda *_: ({key: row}, {key: items}, 0))
    verifier.audit_terminal(lab, session, key, httpx.Response(200 if outcome == 'completed' else 409, json=body), 'crash')
    assert lab.observations['recovery_audits'][0]['terminal_valid'] is True


@pytest.mark.parametrize('defect', [None, 'identity', '504', 'transport', 'malformed'])
def test_strict_sample_is_one_attempt_and_receipt_never_in_numeric_archive(monkeypatch, defect):
    key, ready, session, body, _, _ = bindings()
    body['extra'] = 'PRIVATE-RESPONSE'
    if defect == 'identity': body['request_id'] = str(uuid4())
    calls = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['timeout'] == 10 and kwargs['trust_env'] is False
        def post(self, *args, **kwargs):
            calls.append(kwargs)
            if defect == 'transport': raise httpx.ReadTimeout('PRIVATE-ERROR')
            if defect == 'malformed': return httpx.Response(200, content=b'PRIVATE-MALFORMED')
            return httpx.Response(504 if defect == '504' else 200, json=body)
        def close(self): pass
    monkeypatch.setattr(profiler.httpx, 'Client', Client)
    receipt = {}
    result = profiler.sample(ready['url'], ready, session, 0, 'load', request_key=key, receipt=receipt)
    assert len(calls) == 1 and calls[0]['headers']['Idempotency-Key'] == key
    assert result['response_identity_valid'] is (True if defect is None else False if defect in ('identity', 'malformed') else None)
    assert bool(receipt) is (defect is None)
    assert all(value not in json.dumps(result) for value in ('PRIVATE', key, session['session_id']))


def load_records():
    rows = [dict(sample=i, phase='load', status_code=200, response_identity_valid=True,
                 client_create_ms=1., exchange_ms=10., validation_ms=.1, close_ms=.1, elapsed_ms=11.3,
                 durable_status='completed', durable_item_count=10, durable_result_valid=True,
                 lease_released=True) for i in range(120)]
    return dict(concurrency=2, mode='bounded_closed_loop_no_retry', observations=rows, gate_failure_count=0, durable_request_count=120,
                accepted_rows_remaining=0, **verifier.summarize(rows, 30.))


@pytest.mark.parametrize('defect', ['504', 'transport', 'identity', 'durable', 'lease', 'count', 'accepted',
                                   'missing', 'duplicate', 'summary', 'bool_count', 'timing', 'durable_count'])
def test_load_gate_cannot_accept_forged_green_or_missing_denominator(defect):
    load = load_records()
    row = load['observations'][99]
    if defect == '504': row['status_code'] = 504
    elif defect == 'transport': row.update(status_code=None, transport_error=True)
    elif defect == 'identity': row['response_identity_valid'] = False
    elif defect == 'durable': row['durable_result_valid'] = False
    elif defect == 'lease': row['lease_released'] = False
    elif defect == 'count': row['durable_item_count'] = 9
    elif defect == 'accepted': load['accepted_rows_remaining'] = 1
    elif defect == 'missing': load['observations'].pop()
    elif defect == 'duplicate': row['sample'] = 98
    elif defect == 'summary': load['failures'] = 1
    elif defect == 'bool_count': load['accepted_rows_remaining'] = False
    elif defect == 'timing': row['elapsed_ms'] = float('nan')
    elif defect == 'durable_count': load['durable_request_count'] = 119
    with pytest.raises(gates.GateError): gates.check_load(load, 120)


@pytest.mark.parametrize('defect', [None, '504', 'transport', 'identity', 'durable', 'accepted'])
def test_strict_measure_preserves_all_120_before_rejecting_and_never_archives_receipts(monkeypatch, defect):
    _, ready, session, body, row, items = bindings()
    lab = SimpleNamespace(ready=ready, isolated_url='PRIVATE-DB', observations={})
    calls = []
    def sample(url, ready, session, index, phase, *, request_key, receipt):
        calls.append((index, request_key))
        record = deepcopy(load_records()['observations'][index])
        receipt['body'] = dict(body, request_id=request_key)
        if index == 99:
            if defect == '504': record.update(status_code=504, response_identity_valid=None); receipt.clear()
            elif defect == 'transport': record.update(status_code=None, transport_error=True, response_identity_valid=None); receipt.clear()
            elif defect == 'identity': record['response_identity_valid'] = False; receipt.clear()
        return record
    def ledger(url, keys):
        rows = {key: dict(row, request_id=key) for key in keys}
        if defect == 'durable': rows[keys[99]]['status'] = 'accepted'
        return rows, {key: deepcopy(items) for key in keys}, int(defect == 'accepted')
    monkeypatch.setattr(profiler, 'sample', sample)
    monkeypatch.setattr(verifier, 'read_ledger', ledger)
    if defect is None:
        gates.check_load(verifier.strict_measure_load(lab, session, 120, 2), 120)
    else:
        with pytest.raises(gates.GateError): verifier.strict_measure_load(lab, session, 120, 2)
    load = lab.observations['load']
    assert len(calls) == len(load['observations']) == 120 and len({key for _, key in calls}) == 120
    assert load['failures'] == int(defect in ('504', 'transport'))
    assert 'PRIVATE' not in json.dumps(lab.observations)
    assert all(key not in json.dumps(lab.observations) for _, key in calls)


def documents():
    run_id, schema = str(uuid4()), 'test_evorec_' + uuid4().hex
    audits = [dict(phase=phase, terminal_valid=True, status='failed' if phase == 'crash' else 'completed',
                   item_count=0 if phase == 'crash' else 10, lease_released=True)
              for phase in ('initial', 'restart', 'duplicate', 'disconnect', 'crash', 'post_crash')]
    base = dict(load=load_records(), model_version=MODEL, item_count=137249, recovery_audits=audits,
                restart_exact_replay=True, duplicate_key_single_result=True, disconnect_observed=True,
                disconnect_server_admission_observed=True, hard_crash_admission_observed=True,
                explicit_new_key_after_crash_succeeded=True, recovery_durable_checks_passed=True,
                disconnect_terminal_status=200, crash_outcome='interrupted_without_partial_result')
    report = dict(base, status='passed_local_load_recovery_gates_not_sla', source_commit='a' * 40,
                  source_sha256={'src/a.py': 'b' * 64}, bundle_id=BUNDLE, manifest_sha256=MANIFEST,
                  api_deadline_seconds=2.0, strict_load=True, server_instrumentation_enabled=False,
                  owned_schema_removed=True, production_acceptance=False, sla_proven=False,
                  load_automatic_retries=0, client_connections='fresh_httpx_client_per_sample')
    docs = dict(verification=report, observations=dict(result=deepcopy(base), partial=dict(phase='crash_reconciled', load=deepcopy(base['load'])),
                failure_check=None, source_commit='a' * 40, owned_schema_removed=True),
                owned=dict(run_id=run_id, schema=schema, ephemeral=True),
                stopped=dict(run_id=run_id, owned_schema_removed=True, processes=[dict(attempt=n,
                             hard_killed=n == 2, normal_cpu_drain=n != 2) for n in (1, 2, 3)]))
    for n in (1, 2, 3):
        docs[f'process-{n}/ready'] = dict(bundle_id=BUNDLE, manifest_sha256=MANIFEST, model_version=MODEL,
            item_count=137249, api_deadline_seconds=2., run_id=run_id, schema=schema, backend='numpy',
            admin_enabled=False, ephemeral=True, gc_events_enabled=False, phase_gate_enabled=False,
            pid=100 + n, url=f'http://127.0.0.1:{9000+n}')
        if n != 2: docs[f'process-{n}/stopped'] = dict(run_id=run_id, cpu_jobs_drained=True)
    return docs


@pytest.mark.parametrize('defect', [None, 'source', 'model', 'trace', 'crash', 'drain', 'recovery', 'audit',
                                   'mirror', 'missing', 'protected', 'SLA', 'bool_retry'])
def test_independent_checker_requires_raw_round_identity_and_recovery(tmp_path, defect):
    docs = documents()
    if defect == 'source': docs['verification']['source_commit'] = 'f' * 40
    elif defect == 'model': docs['process-2/ready']['model_version'] = 'other'
    elif defect == 'trace': docs['process-2/ready']['gc_events_enabled'] = True
    elif defect == 'crash': docs['stopped']['processes'][1]['hard_killed'] = False
    elif defect == 'drain': docs['process-3/stopped']['cpu_jobs_drained'] = False
    elif defect == 'recovery': docs['verification']['hard_crash_admission_observed'] = False
    elif defect == 'audit': docs['verification']['recovery_audits'].pop()
    elif defect == 'mirror': docs['observations']['partial']['load']['observations'].pop()
    elif defect == 'missing': del docs['process-3/ready']
    elif defect == 'protected': docs['process-1/ready']['url'] = 'http://127.0.0.1:8000'
    elif defect == 'SLA': docs['verification']['production_acceptance'] = True
    elif defect == 'bool_retry': docs['verification']['load_automatic_retries'] = False
    for name, data in docs.items():
        target = tmp_path / (name + '.json')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data), encoding='utf-8')
    if defect is None:
        result = gates.check_round(tmp_path, 'a' * 40, {'src/a.py': 'b' * 64})
        assert result['successful'] == 120 and result['live_resource_checks_performed'] is False
        assert not result['production_acceptance'] and not result['sla_proven']
    else:
        with pytest.raises((ValueError, OSError)): gates.check_round(tmp_path, 'a' * 40, {'src/a.py': 'b' * 64})


def test_real_ledger_read_is_independent_and_detects_the_actual_advisory_lock(isolated_database, tmp_path):
    root, target, digest = _build(tmp_path)
    output = Path(__file__).resolve().parents[1] / 'artifacts' / 'test-stable-recovery' / uuid4().hex
    lab = R06ServiceLab(output, isolated_database, root, UUID(target.name), digest, 'stdlib')
    with lab:
        with httpx.Client(base_url=lab.ready['url'], trust_env=False, timeout=10) as client:
            session = client.post('/api/v1/sessions', json=dict(profile_id='sample')).json()
            key = str(uuid4())
            reply = client.post('/api/v1/recommendations', headers={'X-Session-Token': session['access_token'],
                                'Idempotency-Key': key}, json=dict(session_id=session['session_id'],
                                expected_history_version=0, strategy='dense', k=2))
            assert reply.status_code == 200
        rows, items, accepted = verifier.read_ledger(lab.isolated_url, [key])
        assert rows[key]['status'] == 'completed' and rows[key]['lease_released'] is True
        assert accepted == 0 and [i['position'] for i in items[key]] == [1, 2]
        assert [(i['item_id'], i['score'], i['source']) for i in items[key]] == [
            (i['item_id'], i['score'], i['source']) for i in reply.json()['items']]
        with psycopg.connect(lab.isolated_url, autocommit=True) as connection:
            lock = connection.execute("SELECT hashtextextended('evorec:recommendation:' || current_schema() || ':' || %s, 0)", (key,)).fetchone()[0]
            connection.execute('SELECT pg_advisory_lock(%s)', (lock,))
            assert verifier.read_ledger(lab.isolated_url, [key])[0][key]['lease_released'] is False
        assert verifier.read_ledger(lab.isolated_url, [key])[0][key]['lease_released'] is True
    assert not lab.created
