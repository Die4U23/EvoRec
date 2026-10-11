"""Strict recorded local load/recovery gates; no requests and no SLA claim."""

import argparse
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID

ARCHIVE_FIELDS = ('status', 'source_commit', 'source_sha256', 'manifest_sha256', 'bundle_id',
                  'api_deadline_seconds', 'owned_schema_removed', 'sla_proven', 'environment',
                  'client_connections', 'load_automatic_retries', 'strict_load',
                  'server_instrumentation_enabled', 'production_acceptance')

class GateError(ValueError):
    """Only fixed assertion labels may be reported."""


def require(condition, label):
    if not condition:
        raise GateError(label)


def strict_legal(body, ready, session, key):
    """Validate the actual caller binding, not just model/item labels."""
    try:
        if not (type(body) is dict and body['request_id'] == key
                and body['session_id'] == session['session_id']
                and body['bundle_id'] == ready['bundle_id']
                and body['model_version'] == ready['model_version']
                and body['requested_strategy'] == body['actual_strategy'] == 'dense'
                and body['fallback_reason'] is None
                and type(body['session_epoch']) is int and body['session_epoch'] == session['epoch']
                and type(body['history_version']) is int and body['history_version'] == session['history_version']
                and type(body['exclusion_version']) is int and body['exclusion_version'] >= 0
                and type(body['captured_at_ms']) is int and body['captured_at_ms'] > 0
                and type(body['items']) is list and len(body['items']) == 10):
            return False
        ids = []
        for item in body['items']:
            if not (type(item) is dict and type(item['item_id']) is str and item['item_id']
                    and item['source'] == 'r06-a-frozen-s17'
                    and type(item['score']) in (int, float) and math.isfinite(item['score'])
                    and item['item_id'] not in session['history']):
                return False
            ids.append(item['item_id'])
        return len(set(ids)) == 10
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def binding_matches(row, ready, session, key):
    try:
        model = row['model_snapshot']
        return (str(row['request_id']) == key and str(row['session_id']) == session['session_id']
                and row['session_epoch'] == session['epoch']
                and row['history_version'] == session['history_version']
                and str(row['bundle_id']) == ready['bundle_id']
                and row['requested_strategy'] == 'dense' and row['requested_k'] == 10
                and type(model) is dict and model['manifest_sha256'] == ready['manifest_sha256']
                and model['model_version'] == ready['model_version']
                and type(model['timestamp_ms']) is int and model['timestamp_ms'] > 0
                and row['execution_owner'] is not None and row['lease_released'] is True)
    except (KeyError, TypeError):
        return False


def durable_matches(body, row, items, ready, session, key):
    """Independent connection's ledger and exact ordered items must match HTTP."""
    try:
        return (strict_legal(body, ready, session, key) and binding_matches(row, ready, session, key)
                and row['status'] == 'completed'
                and all(row[name] == body[name] for name in
                        ('exclusion_version', 'actual_strategy', 'fallback_reason'))
                and row['model_snapshot']['timestamp_ms'] == body['captured_at_ms']
                and len(items) == 10
                and all(item['position'] == index + 1
                        and item['approved_member'] is True
                        and (item['item_id'], item['score'], item['source']) ==
                            (response['item_id'], response['score'], response['source'])
                        and item['item_id'] not in row['model_snapshot']['full_seen']
                        for index, (item, response) in enumerate(zip(items, body['items']))))
    except (KeyError, TypeError):
        return False


def check_load(load, samples):
    from scripts.verify_r06_reliability import summarize
    require(type(samples) is int and samples > 0, 'expected sample count')
    require(type(load) is dict and load.get('concurrency') == 2
            and type(load.get('concurrency')) is int
            and load.get('mode') == 'bounded_closed_loop_no_retry', 'load settings')
    records = load.get('observations')
    require(type(records) is list and len(records) == samples
            and all(type(r) is dict and type(r.get('sample')) is int for r in records)
            and [r['sample'] for r in records] == list(range(samples)), 'sample coverage')
    for record in records:
        require(record.get('phase') == 'load' and type(record.get('status_code')) is int
                and record['status_code'] == 200 and record.get('transport_error', False) is False,
                'load HTTP failure')
        require(record.get('response_identity_valid') is True, 'load response identity')
        require(record.get('durable_result_valid') is True and record.get('lease_released') is True
                and record.get('durable_status') == 'completed'
                and type(record.get('durable_item_count')) is int and record['durable_item_count'] == 10,
                'load durable result')
        for name in ('client_create_ms', 'exchange_ms', 'validation_ms', 'close_ms', 'elapsed_ms'):
            require(type(record.get(name)) in (int, float) and math.isfinite(record[name])
                    and record[name] >= 0, 'client timing')
    require(load.get('accepted_rows_remaining') == 0
            and type(load.get('accepted_rows_remaining')) is int, 'accepted requests remain')
    require(type(load.get('gate_failure_count')) is int and load['gate_failure_count'] == 0, 'gate failure count')
    require(type(load.get('durable_request_count')) is int and load['durable_request_count'] == samples, 'durable request coverage')
    summary = summarize(records, load['wall_seconds'])
    # JSON equality preserves types: True cannot stand in for 1.
    require(json.dumps({k: load.get(k) for k in summary}, sort_keys=True, allow_nan=False)
            == json.dumps(summary, sort_keys=True, allow_nan=False), 'load summary differs')
    return summary


def check_round(directory, commit, hashes):
    # Imports here avoid a cycle with the diagnostic sample's opt-in validator.
    from scripts.check_r06_trace_abba import BUNDLE, MANIFEST, MODEL, read, same_json
    require(re.fullmatch(r'[0-9a-f]{40}', commit) is not None and bool(hashes), 'expected source')
    names = ('verification', 'observations', 'owned', 'stopped',
             'process-1/ready', 'process-1/stopped', 'process-2/ready',
             'process-3/ready', 'process-3/stopped')
    docs, fingerprints = {}, {}
    for name in names:
        docs[name], fingerprints[name + '.json'] = read(directory / (name + '.json'))
        require(type(docs[name]) is dict, 'document type')
    report, obs = docs['verification'], docs['observations']
    owned, stopped = docs['owned'], docs['stopped']
    require(report.get('source_commit') == commit == obs.get('source_commit')
            and report.get('source_sha256') == hashes, 'source fingerprint')
    require(report.get('status') == 'passed_local_load_recovery_gates_not_sla'
            and obs.get('failure_check') is None and same_json(obs.get('result'),
                {k: v for k, v in report.items() if k not in ARCHIVE_FIELDS})
            and obs.get('partial', {}).get('phase') == 'crash_reconciled'
            and same_json(obs.get('partial', {}).get('load'), report.get('load')),
            'observations differ')
    for field, expected in (('strict_load', True), ('server_instrumentation_enabled', False),
                            ('owned_schema_removed', True), ('production_acceptance', False), ('sla_proven', False)):
        require(report.get(field) is expected, 'report flags')
    require(obs.get('owned_schema_removed') is True and report.get('load_automatic_retries') == 0
            and type(report.get('load_automatic_retries')) is int
            and report.get('client_connections') == 'fresh_httpx_client_per_sample', 'client or cleanup flags')
    run_id, schema = owned.get('run_id'), owned.get('schema')
    require(type(run_id) is str and str(UUID(run_id)) == run_id
            and type(schema) is str and re.fullmatch(r'test_evorec_[0-9a-f]{32}', schema) is not None
            and owned.get('ephemeral') is True, 'owned identity')
    for doc in (report, *(docs[f'process-{n}/ready'] for n in (1, 2, 3))):
        require(doc.get('bundle_id') == BUNDLE and doc.get('manifest_sha256') == MANIFEST
                and doc.get('model_version') == MODEL and type(doc.get('item_count')) is int
                and doc['item_count'] == 137249 and doc.get('api_deadline_seconds') == 2.0
                and type(doc.get('api_deadline_seconds')) in (int, float), 'model or deadline')
    pids, ports = [], []
    for n in (1, 2, 3):
        ready = docs[f'process-{n}/ready']
        address = urlsplit(ready['url'])
        require(ready.get('run_id') == run_id and ready.get('schema') == schema
                and ready.get('backend') == 'numpy' and ready.get('admin_enabled') is False
                and ready.get('ephemeral') is True and ready.get('gc_events_enabled') is False
                and ready.get('phase_gate_enabled') is False and ready.get('profile_samples', 0) == 0
                and type(ready.get('pid')) is int and ready['pid'] > 0
                and address.scheme == 'http' and address.hostname == '127.0.0.1'
                and address.port not in (None, 0, 8000) and address.username is None
                and not (address.path or address.query or address.fragment), 'process identity')
        pids.append(ready['pid']); ports.append(address.port)
    require(stopped.get('run_id') == run_id and stopped.get('owned_schema_removed') is True
            and same_json(stopped.get('processes'), [dict(attempt=n, hard_killed=n == 2,
                normal_cpu_drain=n != 2) for n in (1, 2, 3)]), 'process lifecycle')
    for n in (1, 3):
        require(docs[f'process-{n}/stopped'].get('run_id') == run_id
                and docs[f'process-{n}/stopped'].get('cpu_jobs_drained') is True, 'CPU drain')
    check_load(report.get('load'), 120)
    for flag in ('restart_exact_replay', 'duplicate_key_single_result', 'disconnect_observed',
                 'disconnect_server_admission_observed', 'hard_crash_admission_observed',
                 'explicit_new_key_after_crash_succeeded', 'recovery_durable_checks_passed'):
        require(report.get(flag) is True, 'recovery coverage')
    audits = report.get('recovery_audits')
    require(type(audits) is list and [a.get('phase') for a in audits] ==
            ['initial', 'restart', 'duplicate', 'disconnect', 'crash', 'post_crash']
            and all(a.get('terminal_valid') is True and a.get('lease_released') is True
                    and type(a.get('item_count')) is int
                    and a.get('item_count') == (10 if a.get('status') == 'completed' else 0)
                    and a.get('status') in ('completed', 'failed', 'expired') for a in audits), 'recovery durable coverage')
    require(report.get('crash_outcome') in ('interrupted_without_partial_result', 'completed_before_kill')
            and report.get('disconnect_terminal_status') in (200, 409), 'recovery terminal state')
    require(audits[4]['status'] == ('completed' if report['crash_outcome'] == 'completed_before_kill' else 'failed')
            and (audits[3]['status'] == 'completed') == (report['disconnect_terminal_status'] == 200), 'recovery outcome differs')
    return dict(status='recorded_local_load_recovery_gates_checked_not_sla', source_commit=commit,
                run_id=run_id, schema=schema, pids=pids, ports=ports, artifact_sha256=fingerprints,
                successful=120, failures=0, live_resource_checks_performed=False,
                production_acceptance=False, sla_proven=False)


def main(argv=None):
    from scripts.assemble_r06_bundle import _source
    from scripts.verify_r06_reliability import subprocess_sources
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--expected-commit', required=True)
    args = parser.parse_args(argv)
    try:
        project = Path(__file__).resolve().parents[1]
        require(_source(project) == args.expected_commit, 'clean source')
        hashes = subprocess_sources(project)
        result = check_round(args.directory, args.expected_commit, hashes)
        require(_source(project) == args.expected_commit and subprocess_sources(project) == hashes, 'source changed')
    except (ValueError, TypeError, KeyError, OSError, OverflowError) as error:
        print(json.dumps(dict(status='rejected', failed_check=str(error) if type(error) is GateError else type(error).__name__)))
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
