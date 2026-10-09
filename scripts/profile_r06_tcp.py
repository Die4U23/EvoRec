"""Bounded real TCP diagnostic. Never a performance acceptance/SLA."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
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


def _valid_server_start_offset(request):
    if type(request) is not dict:
        return False
    offset = request.get("asgi_start_offset_seconds")
    return (type(offset) in (int, float) and offset >= 0
            and (type(offset) is int or math.isfinite(offset)))


def profile(output, database_url, root, identity, digest, *, samples=24, concurrency=2, gc_events=True,
            phase_gate=False, trace=True):
    if (type(samples) is not int or not 2 <= samples <= 120
            or type(concurrency) is not int or not 1 <= concurrency <= 8):
        raise ValueError("samples 2..120 and concurrency 1..8 required")
    if type(gc_events) is not bool:
        raise ValueError("gc_events must be a bool")
    if type(phase_gate) is not bool:
        raise ValueError("phase_gate must be a bool")
    if type(trace) is not bool:
        raise ValueError("trace must be a bool")
    if not trace and (gc_events or phase_gate):
        raise ValueError("untraced diagnostics require gc_events=False and phase_gate=False")
    project = Path(__file__).resolve().parents[1]
    commit, hashes = _source(project), subprocess_sources(project)
    lab = R06ServiceLab(output, database_url, root, identity, digest, profile_samples=samples+2 if trace else 0,
                        gc_events=gc_events, phase_gate=phase_gate)
    records, result = [], None
    try:
        with lab:
            if not trace and (lab.ready.get("gc_events_enabled") is not False
                              or lab.ready.get("phase_gate_enabled") is not False
                              or type(lab.ready.get("profile_samples", 0)) is not int
                              or lab.ready.get("profile_samples", 0) != 0):
                raise ValueError("owned API untraced settings changed")
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
                   status="instrumented_observations_only" if trace else "uninstrumented_observations_only",
                   owned_schema_removed=not lab.created))
    if _source(project) != commit or subprocess_sources(project) != hashes:
        raise ValueError("source changed during diagnostic")
    client_indices = [record.get("sample") for record in records]
    client_observations_complete = (len(records) == samples+2
                                    and all(type(index) is int for index in client_indices)
                                    and set(client_indices) == set(range(samples+2)))
    successful_client_requests = [record for record in records if record.get("status_code") == 200]
    identities_valid = (all(record["response_identity_valid"] is True
                            for record in successful_client_requests) if successful_client_requests else None)
    if not trace:
        result.update(status="uninstrumented_diagnostic_completed_not_performance_acceptance",
                      source_commit=commit, source_sha256=hashes, requests=records,
                      client_observations_complete=client_observations_complete,
                      successful_identities_valid=identities_valid,
                      trace_enabled=False, server_instrumentation_enabled=False,
                      client_phase_timing_enabled=True, client_observation_overhead_not_subtracted=True,
                      server_requests=None, trace_complete=None, shared_server_timeline_complete=None,
                      client_status_matches_server=None, successful_database_trace_count=None,
                      successful_database_traces_complete=None, database_driver_version=None,
                      database_driver_operations_traced=False,
                      database_execute_includes_driver_lock_wait_network_and_result_receive=None,
                      database_fetch_includes_driver_decode_row_factory_and_python_materialization=None,
                      database_transaction_exit_overlaps_commit_and_close=None,
                      pure_sql_execution_or_exact_database_lock_wait_measured=None,
                      api_deadline_seconds=2.0, owned_schema_removed=True, automatic_retries=0,
                      timings_overlap_do_not_sum=None, instrumentation_overhead_not_subtracted=True,
                      gc_events_enabled=False, gc_attribution_not_exclusive=None,
                      phase_gate_enabled=False, diagnostic_intervention_changes_scheduling=False,
                      production_acceptance=False, sla_proven=False)
        marker(lab.output, "profile", result)
        return result
    server_trace = json.loads((lab.child_output / "profile.json").read_bytes())
    if server_trace.get("gc_events_enabled") is not gc_events:
        raise ValueError("owned API GC event setting changed")
    if server_trace.get("phase_gate_enabled") is not phase_gate:
        raise ValueError("owned API phase_gate setting changed")
    server = server_trace["requests"]
    shared_server_timeline_complete = bool(server) and all(
        _valid_server_start_offset(request) for request in server)
    indices = {record["sample"] for record in server}
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
                  client_observations_complete=client_observations_complete,
                  trace_enabled=True, server_instrumentation_enabled=True,
                  client_phase_timing_enabled=True, client_observation_overhead_not_subtracted=True,
                  trace_complete=len(server) == samples+2 and indices == set(range(samples+2)),
                  shared_server_timeline_complete=shared_server_timeline_complete,
                  client_status_matches_server=all(next((s["status_code"] for s in server
                        if s["sample"] == c["sample"]), None) == c["status_code"] for c in records),
                  successful_identities_valid=identities_valid,
                  api_deadline_seconds=2.0, owned_schema_removed=True, automatic_retries=0,
                  timings_overlap_do_not_sum=True, instrumentation_overhead_not_subtracted=True,
                  gc_events_enabled=gc_events, gc_attribution_not_exclusive=True,
                  phase_gate_enabled=phase_gate,
                  diagnostic_intervention_changes_scheduling=phase_gate,
                  production_acceptance=False, sla_proven=False)
    result.update(database_driver_version=server_trace.get("database_driver_version"),
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
    parser.add_argument("--phase-gate", action="store_true",
                        help="diagnostic intervention, not a production optimization or SLA test")
    parser.add_argument("--untraced", dest="trace", action="store_false",
                        help="disable server tracing; requires --no-gc-events and no --phase-gate")
    parser.set_defaults(gc_events=True)
    args = parser.parse_args(argv)
    if args.trace is False and (args.gc_events is not False or args.phase_gate is not False):
        print(json.dumps(dict(status="failed", error_type="ValueError")))
        return 1
    try:
        result = profile(args.output, os.environ["EVOREC_DATABASE_URL"], args.managed_root, args.bundle_id,
                          args.expected_manifest_sha256, samples=args.samples, concurrency=args.concurrency,
                          gc_events=args.gc_events, phase_gate=args.phase_gate, trace=args.trace)
    except Exception as error:
        print(json.dumps(dict(status="failed", error_type=type(error).__name__)))
        return 1
    success_count = result.get("successful_database_trace_count")
    coverage = result.get("successful_database_traces_complete")
    identities_valid = result.get("successful_identities_valid")
    print(json.dumps(dict(status=result.get("status"), output=str(args.output), trace_complete=result.get("trace_complete"),
                           successful_database_trace_count=success_count)))
    if not args.trace:
        client_records = result.get("requests")
        expected_indices = set(range(args.samples+2))
        client_rows_valid = (type(client_records) is list
                             and len(client_records) == args.samples+2
                             and all(type(record) is dict and type(record.get("sample")) is int
                                     and "status_code" in record
                                     and ((type(record["status_code"]) is int
                                           and 100 <= record["status_code"] <= 599
                                           and record.get("transport_error") is not True)
                                          or (record["status_code"] is None
                                              and record.get("transport_error") is True))
                                     for record in client_records)
                             and {record["sample"] for record in client_records} == expected_indices)
        client_successes = (sum(record["status_code"] == 200 for record in client_records)
                            if client_rows_valid else None)
        successful_rows_valid = (client_rows_valid and all(
            record.get("response_identity_valid") is True
            for record in client_records if record["status_code"] == 200))
        unknown_server_fields = (
            "server_requests", "trace_complete", "shared_server_timeline_complete",
            "client_status_matches_server", "successful_database_trace_count",
            "successful_database_traces_complete", "database_driver_version",
            "database_execute_includes_driver_lock_wait_network_and_result_receive",
            "database_fetch_includes_driver_decode_row_factory_and_python_materialization",
            "database_transaction_exit_overlaps_commit_and_close",
            "pure_sql_execution_or_exact_database_lock_wait_measured",
            "timings_overlap_do_not_sum", "gc_attribution_not_exclusive",
        )
        untraced_gate = (result.get("status") == "uninstrumented_diagnostic_completed_not_performance_acceptance"
                         and result.get("trace_enabled") is False
                         and result.get("server_instrumentation_enabled") is False
                         and result.get("client_phase_timing_enabled") is True
                         and result.get("client_observations_complete") is True
                         and client_rows_valid and successful_rows_valid
                         and result.get("owned_schema_removed") is True
                         and result.get("gc_events_enabled") is False
                         and result.get("phase_gate_enabled") is False
                         and result.get("database_driver_operations_traced") is False
                         and all(field in result and result[field] is None for field in unknown_server_fields)
                         and ((client_successes == 0 and identities_valid is None)
                              or (client_successes is not None and client_successes > 0
                                  and identities_valid is True))
                         and result.get("production_acceptance") is False
                         and result.get("sla_proven") is False)
        return 0 if untraced_gate else 1
    database_trace_gate = ("successful_database_traces_complete" in result
                           and type(success_count) is int and success_count >= 0
                           and ((success_count == 0 and coverage is None)
                                or (success_count > 0 and coverage is True)))
    identity_gate = ("successful_identities_valid" in result
                     and ((success_count == 0 and identities_valid is None)
                          or (type(success_count) is int and success_count > 0 and identities_valid is True)))
    return 0 if (result.get("status") == "instrumented_diagnostic_completed_not_performance_acceptance"
                  and result.get("trace_complete") and result.get("client_status_matches_server")
                 and result.get("shared_server_timeline_complete") is True
                 and identity_gate and database_trace_gate) else 1


if __name__ == "__main__":
    raise SystemExit(main())
