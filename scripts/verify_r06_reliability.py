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

from scripts.assemble_r06_bundle import _source
from scripts.r06_service_lab import R06ServiceLab
from scripts.run_r06_demo import marker


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


def require(condition, message):
    if not condition:
        raise ValueError(message)


def exercise(lab, samples, concurrency):
    lab.observations = dict(phase="initial", load=None)
    with httpx.Client(base_url=lab.ready["url"], trust_env=False, timeout=10) as client:
        response = client.post("/api/v1/sessions", json={"profile_id": "sample"})
        require(response.status_code == 201, "sample creation failed")
        session = response.json()
    key = str(uuid4())
    response, latency = post(lab.ready["url"], session, key)
    require(response.status_code == 200 and legal(response.json(), lab.ready, session), "first dense failed")
    original = response.json()
    lab.stop()
    lab.start()
    response, replay_latency = post(lab.ready["url"], session, key)
    require(response.status_code == 200 and response.json() == original, "restart replay differs")
    lab.observations.update(phase="restart_verified", restart_exact_replay=True)
    with httpx.Client(base_url=lab.ready["url"], trust_env=False, timeout=10) as client:
        restored = client.get("/api/v1/sessions/"+session["session_id"],
                              headers={"X-Session-Token": session["access_token"]})
        require(restored.status_code == 200 and restored.json()["history"] == session["history"],
                "restart history differs")
    # A single logical request concurrently arriving twice must persist once.
    same_key = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        duplicates = list(pool.map(lambda _: post(lab.ready["url"], session, same_key), range(2)))
    for reply, _ in duplicates:
        require(reply.status_code == 200 or (reply.status_code == 409
                and reply.json()["error"]["code"] == "recommendation_in_progress"), "duplicate race differs")
    final, _ = post(lab.ready["url"], session, same_key)
    require(final.status_code == 200 and legal(final.json(), lab.ready, session), "duplicate did not complete")
    for reply, _ in duplicates:
        if reply.status_code == 200:
            require(reply.json() == final.json(), "duplicate output differs")

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
    start = perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        records = list(pool.map(sample, range(samples)))
    duration = perf_counter()-start
    lab.observations.update(phase="load_measured", load=dict(concurrency=concurrency,
        mode="bounded_closed_loop_no_retry", **summarize(records, duration), observations=records))
    # Client disconnect with the same original key; transport failure is not server failure.
    disconnected_key = str(uuid4())
    disconnected = False
    try:
        post(lab.ready["url"], session, disconnected_key, timeout=.05)
    except httpx.HTTPError:
        disconnected = True
    sleep(3)
    recovered, _ = post(lab.ready["url"], session, disconnected_key)
    require(recovered.status_code == 200 or (recovered.status_code == 409
            and not recovered.json()["error"]["retryable"]), "disconnect original key is not terminal")
    if recovered.status_code == 200:
        require(legal(recovered.json(), lab.ready, session), "disconnect replay identity differs")

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
                   and crash_replay.json()["error"]["code"] == "recommendation_interrupted")
    require(interrupted or (crash_replay.status_code == 200
            and legal(crash_replay.json(), lab.ready, session)), "crash replay is not safe terminal state")
    again, _ = post(lab.ready["url"], session, crash_key)
    require(again.json() == crash_replay.json(), "crash terminal replay differs")
    lab.observations.update(phase="crash_reconciled",
                           crash_outcome="interrupted_without_partial_result" if interrupted else "completed_before_kill")
    with psycopg.connect(lab.isolated_url) as connection:
        count = connection.execute("SELECT count(*) FROM request_items WHERE request_id=%s", (crash_key,)).fetchone()[0]
        require(count == (0 if interrupted else 10), "partial crash result persisted")
        require(connection.execute("SELECT count(*) FROM recommendation_requests WHERE request_id=%s",
                                   (same_key,)).fetchone()[0] == 1, "duplicate request persisted twice")
    new, _ = post(lab.ready["url"], session, str(uuid4()))
    require(new.status_code == 200 and legal(new.json(), lab.ready, session), "new post-crash dense failed")
    return dict(restart_exact_replay=True, first_dense_ms=latency, restart_replay_ms=replay_latency,
        item_count=lab.ready['item_count'], model_version=lab.ready['model_version'],
        profile='fixed_approved_training_seed_not_real_user',
        duplicate_key_single_result=True, disconnect_observed=disconnected,
        disconnect_terminal_status=recovered.status_code, hard_crash_admission_observed=admitted,
        crash_outcome="interrupted_without_partial_result" if interrupted else "completed_before_kill",
        explicit_new_key_after_crash_succeeded=True, load=dict(concurrency=concurrency,
            mode="bounded_closed_loop_no_retry", **summarize(records, duration), observations=records))


def verify(output, database_url, root, identity, digest, *, samples=12, concurrency=2, backend="numpy"):
    if type(samples) is not int or not 2 <= samples <= 1000 or type(concurrency) is not int or not 1 <= concurrency <= 8:
        raise ValueError("samples 2..1000 and concurrency 1..8 required")
    project = Path(__file__).resolve().parents[1]
    commit = _source(project)
    files = subprocess_sources(project)
    lab = R06ServiceLab(output, database_url, root, identity, digest, backend)
    result = None
    try:
        with lab:
            result = exercise(lab, samples, concurrency)
    finally:
        if lab.output.exists():
            marker(lab.output, "observations", dict(status="observations_only", result=result,
                   partial=getattr(lab, "observations", None),
                   source_commit=commit, owned_schema_removed=not lab.created))
    require(_source(project) == commit and subprocess_sources(project) == files, "source changed during acceptance")
    result.update(status="passed_recovery_load_measured_not_sla", source_commit=commit,
                  source_sha256=files, manifest_sha256=digest, bundle_id=str(identity),
                  api_deadline_seconds=2.0, owned_schema_removed=True, sla_proven=False,
                  environment=dict(python=platform.python_version(), platform=platform.platform(),
                                   processor=platform.processor(),logical_cpus=os.cpu_count(), backend=backend),
                  client_connections='fresh_httpx_client_per_sample',load_automatic_retries=0)
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
    args = parser.parse_args()
    try:
        result = verify(args.output, os.environ["EVOREC_DATABASE_URL"], args.managed_root, args.bundle_id,
                        args.expected_manifest_sha256, samples=args.samples, concurrency=args.concurrency)
    except Exception as error:
        print(json.dumps(dict(status="failed", error_type=type(error).__name__)))
        return 1
    print(json.dumps(dict(status=result["status"], output=str(args.output))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
