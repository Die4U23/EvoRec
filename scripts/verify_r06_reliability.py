"""Real-process recovery and bounded closed-loop TCP load; no retries in load samples."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import platform
from time import perf_counter, sleep
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg.rows import dict_row

from scripts.assemble_r06_bundle import _source
from scripts.r06_service_lab import R06ServiceLab
from scripts.run_r06_demo import marker
from scripts.check_r06_stable_recovery import (GateError, binding_matches, check_load,
                                             durable_matches, strict_legal)


def percentiles(values):
    values = sorted(values)
    return {name: values[max(0, math.ceil(len(values)*fraction)-1)] if values else None
            for name, fraction in (("p50_ms", .5), ("p95_ms", .95), ("p99_ms", .99))}


def summarize(records, elapsed):
    if not math.isfinite(elapsed) or elapsed <= 0 or any(
            not math.isfinite(record['elapsed_ms']) or record['elapsed_ms'] < 0 for record in records):
        raise ValueError('finite nonnegative timings and positive wall duration required')
    successful = [r for r in records if r["status_code"] == 200]
    return dict(requests=len(records), successful=len(successful), failures=len(records)-len(successful),
        success_rate=len(successful)/len(records) if records else None,
        wall_seconds=elapsed, completed_requests_per_second=len(records)/elapsed,
        successful_requests_per_second=len(successful)/elapsed,
        all_requests_latency=percentiles([r["elapsed_ms"] for r in records]),
        successful_latency=percentiles([r["elapsed_ms"] for r in successful]),
        status_counts={str(code): sum(r["status_code"] == code for r in records)
                       for code in sorted({r["status_code"] for r in records}, key=str)})


def post(url, session, key, *, timeout=10):
    started = perf_counter()
    with httpx.Client(base_url=url, timeout=timeout, trust_env=False) as client:
        response = client.post("/api/v1/recommendations", headers={
            "X-Session-Token": session["access_token"], "Idempotency-Key": key}, json={
            "session_id": session["session_id"], "expected_history_version": session["history_version"],
            "strategy": "dense", "k": 10})
    return response, (perf_counter()-started)*1000


def legal(body, ready, session):
    return (body["bundle_id"] == ready["bundle_id"] and body["model_version"] == ready["model_version"]
            and body["session_id"] == session["session_id"] and body["actual_strategy"] == "dense"
            and body["fallback_reason"] is None and len(body["items"]) == 10
            and len({item["item_id"] for item in body["items"]}) == 10
            and all(item["source"] == "r06-a-frozen-s17" and item["item_id"] not in session["history"]
                    for item in body["items"]))


class VerificationFailure(ValueError):
    """Only fixed local assertion labels may enter diagnostic reports."""


def terminal_replay_matches(first, again):
    if first.status_code != again.status_code:
        return False
    if first.status_code == 200:
        return first.json() == again.json()
    a, b = first.json()['error'], again.json()['error']
    # Initial orphan reconciliation and later terminal replay use different explanatory text.
    return all(key in a and key in b and a[key] == b[key]
               for key in ('code', 'retryable', 'request_id'))


def require(condition, message):
    if not condition:
        raise VerificationFailure(message)


def read_ledger(database_url, keys):
    """Independent consistent read; inspect advisory locks without acquiring them."""
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        rows = connection.execute("""
            SELECT r.*, NOT EXISTS (
                SELECT 1 FROM pg_locks l WHERE l.locktype='advisory' AND l.granted AND l.objsubid=1
                AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
                AND l.classid::bigint=((k.value >> 32) & 4294967295)
                AND l.objid::bigint=(k.value & 4294967295)) AS lease_released
            FROM recommendation_requests r
            CROSS JOIN LATERAL (SELECT hashtextextended(
                'evorec:recommendation:' || current_schema() || ':' || r.request_id::text, 0) AS value) k
            WHERE r.request_id=ANY(%s)
        """, ([UUID(key) for key in keys],)).fetchall()
        items = connection.execute('SELECT ri.*, EXISTS (SELECT 1 FROM bundle_items bi '
                                   'JOIN recommendation_requests r ON r.bundle_id=bi.bundle_id '
                                   'JOIN items i ON i.item_id=bi.item_id '
                                   'WHERE r.request_id=ri.request_id AND bi.item_id=ri.item_id AND i.is_active) AS approved_member '
                                   'FROM request_items ri WHERE ri.request_id=ANY(%s) ORDER BY ri.request_id,ri.position',
                                   ([UUID(key) for key in keys],)).fetchall()
        accepted = connection.execute("SELECT count(*) AS count FROM recommendation_requests WHERE status='accepted'").fetchone()['count']
    by_key = {str(row['request_id']): row for row in rows}
    grouped = {key: [] for key in keys}
    for item in items:
        grouped[str(item['request_id'])].append(item)
    return by_key, grouped, accepted


def audit_terminal(lab, session, key, response, phase):
    rows, items, accepted = read_ledger(lab.isolated_url, [key])
    row, ordered = rows.get(key), items[key]
    valid = False
    if response.status_code == 200:
        valid = durable_matches(response.json(), row, ordered, lab.ready, session, key)
    elif response.status_code == 409 and row is not None:
        error = response.json().get('error', {})
        valid = (binding_matches(row, lab.ready, session, key)
                 and row['status'] in ('failed', 'expired') and not ordered
                 and error.get('retryable') is False and error.get('request_id') == key
                 and error.get('code') == ('recommendation_interrupted'
                    if row['failure_code'] == 'execution_interrupted' else 'recommendation_idempotency_conflict'))
    record = dict(phase=phase, terminal_valid=valid, status=row['status'] if row else None,
                  item_count=len(ordered), lease_released=row['lease_released'] if row else None)
    lab.observations.setdefault('recovery_audits', []).append(record)
    require(valid, 'recovery ledger differs')
    if phase == 'post_crash':
        require(accepted == 0, 'recovery accepted requests remain')


def strict_measure_load(lab, session, samples, concurrency):
    from scripts.profile_r06_tcp import sample
    keys, receipts = [str(uuid4()) for _ in range(samples)], [{} for _ in range(samples)]
    require(len(set(keys)) == samples, 'load keys not unique')
    records = []
    load = dict(concurrency=concurrency, mode='bounded_closed_loop_no_retry', observations=records)
    lab.observations.update(phase='load_collecting', load=load)
    start = perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for record in pool.map(lambda index: sample(lab.ready['url'], lab.ready, session, index, 'load',
                                  request_key=keys[index], receipt=receipts[index]), range(samples)):
            records.append(record)
    duration = perf_counter() - start
    load.update(**summarize(records, duration))
    lab.observations.update(phase='load_measured')
    rows, items, accepted = read_ledger(lab.isolated_url, keys)
    load['accepted_rows_remaining'] = accepted
    load['durable_request_count'] = len(rows)
    for record, key, receipt in zip(records, keys, receipts):
        row = rows.get(key)
        record.update(durable_status=row['status'] if row else None,
                      durable_item_count=len(items[key]), lease_released=row['lease_released'] if row else None,
                      durable_result_valid=(durable_matches(receipt.get('body'), row, items[key],
                            lab.ready, session, key) if record['status_code'] == 200 else None))
    load['gate_failure_count'] = sum(record['status_code'] != 200 or record.get('transport_error') is True
                                   or record['response_identity_valid'] is not True
                                   or record['durable_result_valid'] is not True for record in records)
    # Only after all observations are preserved: reject any failure, do not retry.
    check_load(load, samples)
    lab.observations.update(phase='load_gates_passed')
    return load


def exercise(lab, samples, concurrency, *, strict_load=False):
    lab.observations = dict(phase="initial", load=None)
    with httpx.Client(base_url=lab.ready["url"], trust_env=False, timeout=10) as client:
        response = client.post("/api/v1/sessions", json={"profile_id": "sample"})
        require(response.status_code == 201, "sample creation failed")
        session = response.json()
    key = str(uuid4())
    valid = lambda body, request_key: (strict_legal(body, lab.ready, session, request_key)
                                     if strict_load else legal(body, lab.ready, session))
    response, latency = post(lab.ready["url"], session, key)
    # Record numeric observations before the assertion; no tokens, URLs,
    # response bodies or arbitrary server text enter the failure report.
    lab.observations.update(first_dense_http_status=response.status_code, first_dense_elapsed_ms=latency)
    require(response.status_code == 200 and valid(response.json(), key), "first dense failed")
    if strict_load:
        audit_terminal(lab, session, key, response, 'initial')
    original = response.json()
    lab.stop()
    lab.start()
    response, replay_latency = post(lab.ready["url"], session, key)
    require(response.status_code == 200 and response.json() == original, "restart replay differs")
    if strict_load:
        audit_terminal(lab, session, key, response, 'restart')
    lab.observations.update(phase="restart_verified", restart_exact_replay=True)
    with httpx.Client(base_url=lab.ready["url"], trust_env=False, timeout=10) as client:
        restored = client.get("/api/v1/sessions/"+session["session_id"],
                              headers={"X-Session-Token": session["access_token"]})
        require(restored.status_code == 200 and restored.json()["history"] == session["history"],
                "restart history differs")
        if strict_load:
            require(all(restored.json()[field] == session[field] for field in
                        ('session_id', 'epoch', 'history_version')), 'restart session binding differs')
    # A single logical request concurrently arriving twice must persist once.
    same_key = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        duplicates = list(pool.map(lambda _: post(lab.ready["url"], session, same_key), range(2)))
    for reply, _ in duplicates:
        require(reply.status_code == 200 or (reply.status_code == 409
                and reply.json()["error"]["code"] == "recommendation_in_progress"), "duplicate race differs")
    final, _ = post(lab.ready["url"], session, same_key)
    require(final.status_code == 200 and valid(final.json(), same_key), "duplicate did not complete")
    for reply, _ in duplicates:
        if reply.status_code == 200:
            require(reply.json() == final.json(), "duplicate output differs")
    if strict_load:
        audit_terminal(lab, session, same_key, final, 'duplicate')

    records = []
    def sample(index):
        sample_started = perf_counter()
        try:
            reply, ms = post(lab.ready["url"], session, str(uuid4()))
            require(reply.status_code != 200 or legal(reply.json(), lab.ready, session), "load identity differs")
            try:
                code = reply.json().get("error", {}).get("code")
            except ValueError:
                code = 'non_json_error_response'
            return dict(sample=index, status_code=reply.status_code, elapsed_ms=ms,
                        error_code=code)
        except httpx.HTTPError as error:
            return dict(sample=index, status_code=None, elapsed_ms=(perf_counter()-sample_started)*1000,
                        error_code=type(error).__name__)
    if strict_load:
        load = strict_measure_load(lab, session, samples, concurrency)
    else:
        start = perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            records = list(pool.map(sample, range(samples)))
        duration = perf_counter()-start
        load = dict(concurrency=concurrency, mode="bounded_closed_loop_no_retry",
                    **summarize(records, duration), observations=records)
        lab.observations.update(phase="load_measured", load=load)
    # Client disconnect with the same original key; transport failure is not server failure.
    disconnected_key = str(uuid4())
    disconnected = False
    try:
        post(lab.ready["url"], session, disconnected_key, timeout=.05)
    except httpx.HTTPError:
        disconnected = True
    sleep(3)
    with psycopg.connect(lab.isolated_url) as connection:
        disconnected_row = connection.execute('SELECT status,model_snapshot FROM recommendation_requests WHERE request_id=%s',
                                              (disconnected_key,)).fetchone()
        disconnected_items = connection.execute('SELECT item_id,score,source FROM request_items WHERE request_id=%s ORDER BY position',
                                                 (disconnected_key,)).fetchall()
    require(disconnected and disconnected_row is not None, 'client disconnect did not establish server admission')
    recovered, _ = post(lab.ready["url"], session, disconnected_key)
    require(recovered.status_code == 200 or (recovered.status_code == 409
            and not recovered.json()["error"]["retryable"]), "disconnect original key is not terminal")
    if recovered.status_code == 200:
        require(valid(recovered.json(), disconnected_key), "disconnect replay identity differs")
        require(disconnected_row[0] == 'completed'
                and recovered.json()['captured_at_ms'] == disconnected_row[1]['timestamp_ms']
                and [(item['item_id'],item['score'],item['source']) for item in recovered.json()['items']] == disconnected_items,
                'disconnect response differs from the original completed ledger')
    if strict_load:
        audit_terminal(lab, session, disconnected_key, recovered, 'disconnect')
    lab.observations.update(phase='disconnect_verified', disconnect_server_admission_observed=True,
                            disconnect_terminal_status=recovered.status_code)

    # Kill the verified real API process while a real dense request is admitted.
    crash_key = str(uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(post, lab.ready["url"], session, crash_key)
        deadline, admitted = perf_counter()+10, False
        while perf_counter() < deadline:
            with psycopg.connect(lab.isolated_url) as connection:
                row = connection.execute("SELECT status, execution_owner FROM recommendation_requests WHERE request_id=%s",
                                         (crash_key,)).fetchone()
            if row and row[0] == "accepted" and row[1] is not None:
                admitted = True
                break
            if work.done():
                break
            sleep(.01)
        require(admitted, "no admitted real request observed before crash")
        lab.stop(crash=True)
        try:
            work.result(timeout=15)
        except httpx.HTTPError:
            pass
    lab.start()
    crash_replay, _ = post(lab.ready["url"], session, crash_key)
    interrupted = (crash_replay.status_code == 409
                   and crash_replay.json()["error"]["code"] == "recommendation_interrupted"
                   and crash_replay.json()['error']['retryable'] is False
                   and crash_replay.json()['error']['request_id'] == crash_key)
    require(interrupted or (crash_replay.status_code == 200
            and valid(crash_replay.json(), crash_key)), "crash replay is not safe terminal state")
    lab.observations.update(phase='crash_terminal_observed', crash_terminal_status=crash_replay.status_code,
                           crash_outcome='interrupted_without_partial_result' if interrupted else 'completed_before_kill')
    again, _ = post(lab.ready["url"], session, crash_key)
    require(terminal_replay_matches(crash_replay, again), "crash terminal replay differs")
    if strict_load:
        audit_terminal(lab, session, crash_key, again, 'crash')
    lab.observations.update(phase="crash_reconciled",
                           crash_outcome="interrupted_without_partial_result" if interrupted else "completed_before_kill")
    with psycopg.connect(lab.isolated_url) as connection:
        count = connection.execute("SELECT count(*) FROM request_items WHERE request_id=%s", (crash_key,)).fetchone()[0]
        require(count == (0 if interrupted else 10), "partial crash result persisted")
        require(connection.execute("SELECT count(*) FROM recommendation_requests WHERE request_id=%s",
                                   (same_key,)).fetchone()[0] == 1, "duplicate request persisted twice")
    new_key = str(uuid4())
    new, _ = post(lab.ready["url"], session, new_key)
    require(new.status_code == 200 and valid(new.json(), new_key), "new post-crash dense failed")
    if strict_load:
        audit_terminal(lab, session, new_key, new, 'post_crash')
    return dict(restart_exact_replay=True, first_dense_ms=latency, restart_replay_ms=replay_latency,
        item_count=lab.ready['item_count'], model_version=lab.ready['model_version'],
        profile='fixed_approved_training_seed_not_real_user',
        duplicate_key_single_result=True, disconnect_observed=disconnected,
        disconnect_server_admission_observed=True,
        disconnect_terminal_status=recovered.status_code, hard_crash_admission_observed=admitted,
        crash_outcome="interrupted_without_partial_result" if interrupted else "completed_before_kill",
        explicit_new_key_after_crash_succeeded=True, load=load,
        recovery_durable_checks_passed=True if strict_load else None,
        recovery_audits=lab.observations.get('recovery_audits'))


def verify(output, database_url, root, identity, digest, *, samples=12, concurrency=2, backend="numpy", strict_load=False):
    if type(samples) is not int or not 2 <= samples <= 1000 or type(concurrency) is not int or not 1 <= concurrency <= 8:
        raise ValueError("samples 2..1000 and concurrency 1..8 required")
    if type(strict_load) is not bool or (strict_load and concurrency != 2):
        raise ValueError('strict_load requires bool and concurrency=2')
    project = Path(__file__).resolve().parents[1]
    commit = _source(project)
    files = subprocess_sources(project)
    lab = R06ServiceLab(output, database_url, root, identity, digest, backend, gc_events=not strict_load)
    result, failure_check = None, None
    try:
        with lab:
            result = exercise(lab, samples, concurrency, strict_load=strict_load)
    except (VerificationFailure, GateError) as error:
        failure_check = str(error)
        raise
    finally:
        if lab.output.exists():
            marker(lab.output, "observations", dict(status="observations_only", result=result,
                   partial=getattr(lab, "observations", None),
                   source_commit=commit, failure_check=failure_check, owned_schema_removed=not lab.created))
    require(_source(project) == commit and subprocess_sources(project) == files, "source changed during acceptance")
    result.update(status="passed_local_load_recovery_gates_not_sla" if strict_load else "passed_recovery_load_measured_not_sla", source_commit=commit,
                  source_sha256=files, manifest_sha256=digest, bundle_id=str(identity),
                  api_deadline_seconds=2.0, owned_schema_removed=True, sla_proven=False,
                  environment=dict(python=platform.python_version(), platform=platform.platform(),
                                   processor=platform.processor(),logical_cpus=os.cpu_count(), backend=backend),
                  client_connections='fresh_httpx_client_per_sample',load_automatic_retries=0,
                  strict_load=strict_load, server_instrumentation_enabled=False, production_acceptance=False)
    marker(lab.output, "verification", result)
    return result


def subprocess_sources(project):
    import subprocess
    names = subprocess.check_output(["git", "ls-files", "src", "scripts", "db/migrations"],
                                    cwd=project, text=True).splitlines()
    return {name: hashlib.sha256((project/name).read_bytes()).hexdigest() for name in names}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("managed_root", type=Path)
    parser.add_argument("bundle_id", type=UUID)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument('--strict-load', action='store_true', help='require zero load failures and independently persisted results')
    args = parser.parse_args()
    try:
        result = verify(args.output, os.environ["EVOREC_DATABASE_URL"], args.managed_root, args.bundle_id,
                        args.expected_manifest_sha256, samples=args.samples, concurrency=args.concurrency,
                        strict_load=args.strict_load)
    except Exception as error:
        print(json.dumps(dict(status="failed", error_type=type(error).__name__)))
        return 1
    print(json.dumps(dict(status=result["status"], output=str(args.output))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
