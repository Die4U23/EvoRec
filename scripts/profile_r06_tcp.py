"""Bounded instrumented real TCP diagnostic. Never a performance acceptance/SLA."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

import httpx

from scripts.assemble_r06_bundle import _source
from scripts.r06_service_lab import R06ServiceLab
from scripts.run_r06_demo import marker
from scripts.verify_r06_reliability import legal, subprocess_sources, summarize


def sample(url, ready, session, index, phase):
    started = perf_counter()
    record = dict(sample=index, phase=phase, status_code=None, response_identity_valid=None,
                  client_create_ms=None, exchange_ms=None, validation_ms=0.0, close_ms=None)
    client = None
    try:
        client = httpx.Client(base_url=url, timeout=10, trust_env=False)
        created = perf_counter()
        record["client_create_ms"] = (created-started)*1000
        response = client.post("/api/v1/recommendations", headers={
            "X-Session-Token": session["access_token"], "Idempotency-Key": str(uuid4()),
            "X-EvoRec-Diagnostic-Sample": str(index)}, json={
            "session_id": session["session_id"], "expected_history_version": session["history_version"],
            "strategy": "dense", "k": 10})
        exchanged = perf_counter()
        record.update(status_code=response.status_code, exchange_ms=(exchanged-created)*1000)
        # Numeric/boolean observations only, never dump server error bodies.
        if response.status_code == 200:
            try:
                record["response_identity_valid"] = legal(response.json(), ready, session)
            except (ValueError, KeyError, TypeError):
                record["response_identity_valid"] = False
        record["validation_ms"] = (perf_counter()-exchanged)*1000
    except httpx.HTTPError:
        record["transport_error"] = True
    finally:
        close_started = perf_counter()
        if client is not None:
            client.close()
        record["close_ms"] = (perf_counter()-close_started)*1000
        record["elapsed_ms"] = (perf_counter()-started)*1000
    return record


def profile(output, database_url, root, identity, digest, *, samples=24, concurrency=2, gc_events=True):
    if (type(samples) is not int or not 2 <= samples <= 120
            or type(concurrency) is not int or not 1 <= concurrency <= 8):
        raise ValueError("samples 2..120 and concurrency 1..8 required")
    if type(gc_events) is not bool:
        raise ValueError("gc_events must be a bool")
    project = Path(__file__).resolve().parents[1]
    commit, hashes = _source(project), subprocess_sources(project)
    lab = R06ServiceLab(output, database_url, root, identity, digest, profile_samples=samples+2,
                        gc_events=gc_events)
    records, result = [], None
    try:
        with lab:
            with httpx.Client(base_url=lab.ready["url"], timeout=10, trust_env=False) as client:
                response = client.post("/api/v1/sessions", json={"profile_id": "sample"})
                if response.status_code != 201:
                    raise ValueError("sample creation failed")
                session = response.json()
            for index in range(2):
                records.append(sample(lab.ready["url"], lab.ready, session, index, "sequential"))
            started = perf_counter()
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                load = list(pool.map(lambda index: sample(lab.ready["url"], lab.ready, session, index, "load"),
                                     range(2, samples+2)))
            duration = perf_counter()-started
            records.extend(load)
            result = dict(load=summarize(load, duration), bundle_id=str(identity), manifest_sha256=digest,
                          model_version=lab.ready["model_version"], item_count=lab.ready["item_count"],
                          concurrency=concurrency)
    finally:
        if lab.output.exists():
            marker(lab.output, "observations", dict(source_commit=commit, requests=records,
                   status="instrumented_observations_only", owned_schema_removed=not lab.created))
    if _source(project) != commit or subprocess_sources(project) != hashes:
        raise ValueError("source changed during diagnostic")
    trace = json.loads((lab.child_output / "profile.json").read_bytes())
    if trace.get("gc_events_enabled") is not gc_events:
        raise ValueError("owned API GC event setting changed")
    server = trace["requests"]
    indices = {record["sample"] for record in server}
    successful_client_requests = [r for r in records if r.get("status_code") == 200]
    successful_server_requests = [r for r in server if r.get("status_code") == 200]
    required_database_stages = {
        "publication_recovery_database_connect", "database_admission_database_connect",
        "database_admission_database_execute_session_snapshot_lock",
        "actual_catalog_read_and_capture_database_execute_actual_catalog_rows",
        "actual_catalog_read_and_capture_database_fetchall_decode",
        "result_write_database_commit", "result_write_database_close",
    }
    result.update(status="instrumented_diagnostic_completed_not_performance_acceptance",
                  source_commit=commit, source_sha256=hashes, requests=records, server_requests=server,
                  trace_complete=len(server) == samples+2 and indices == set(range(samples+2)),
                  client_status_matches_server=all(next((s["status_code"] for s in server
                        if s["sample"] == c["sample"]), None) == c["status_code"] for c in records),
                  successful_identities_valid=(all(c["response_identity_valid"] is True
                        for c in successful_client_requests) if successful_client_requests else None),
                  api_deadline_seconds=2.0, owned_schema_removed=True, automatic_retries=0,
                  timings_overlap_do_not_sum=True, instrumentation_overhead_not_subtracted=True,
                  gc_events_enabled=gc_events, gc_attribution_not_exclusive=True,
                  production_acceptance=False, sla_proven=False)
    result.update(database_driver_version=trace.get("database_driver_version"),
                  successful_database_trace_count=len(successful_server_requests),
                  successful_database_traces_complete=(
                      all(required_database_stages <= {s["stage"] for s in r.get("stages", [])}
                          for r in successful_server_requests) if successful_server_requests else None),
                  database_driver_operations_traced=True,
                  database_execute_includes_driver_lock_wait_network_and_result_receive=True,
                  database_fetch_includes_driver_decode_row_factory_and_python_materialization=True,
                  database_transaction_exit_overlaps_commit_and_close=True,
                  pure_sql_execution_or_exact_database_lock_wait_measured=False)
    marker(lab.output, "profile", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("managed_root", type=Path)
    parser.add_argument("bundle_id", type=UUID)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--samples", type=int, default=24)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--no-gc-events", dest="gc_events", action="store_false",
                        help="omit garbage-collection callback observations")
    parser.set_defaults(gc_events=True)
    args = parser.parse_args(argv)
    try:
        result = profile(args.output, os.environ["EVOREC_DATABASE_URL"], args.managed_root, args.bundle_id,
                         args.expected_manifest_sha256, samples=args.samples, concurrency=args.concurrency,
                         gc_events=args.gc_events)
    except Exception as error:
        print(json.dumps(dict(status="failed", error_type=type(error).__name__)))
        return 1
    success_count = result.get("successful_database_trace_count")
    coverage = result.get("successful_database_traces_complete")
    identities_valid = result.get("successful_identities_valid")
    print(json.dumps(dict(status=result["status"], output=str(args.output), trace_complete=result["trace_complete"],
                          successful_database_trace_count=success_count)))
    database_trace_gate = ("successful_database_traces_complete" in result
                           and type(success_count) is int and success_count >= 0
                           and ((success_count == 0 and coverage is None)
                                or (success_count > 0 and coverage is True)))
    identity_gate = ("successful_identities_valid" in result
                     and ((success_count == 0 and identities_valid is None)
                          or (type(success_count) is int and success_count > 0 and identities_valid is True)))
    return 0 if (result["trace_complete"] and result["client_status_matches_server"]
                 and identity_gate and database_trace_gate) else 1


if __name__ == "__main__":
    raise SystemExit(main())
