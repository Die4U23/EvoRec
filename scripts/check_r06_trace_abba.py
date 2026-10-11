"""Recheck fixed trace OFF/ON/ON/OFF artifacts; never run requests or prove SLA."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID

from scripts.profile_r06_tcp import _fresh_database_trace_complete, _timeout_trace_complete
from scripts.run_r06_demo import marker
from scripts.verify_r06_reliability import percentiles, subprocess_sources, summarize


ORDER = (("a1", False), ("b1", True), ("b2", True), ("a2", False))
BUNDLE = "bfe57ed8-3f6f-429d-ba07-b64b2ab1aeec"
MANIFEST = "9d622c95841027175779b19cbda5e1f0923c574c45d5daa4dab8a84ed8691ca6"
MODEL = "eebd07deded0d2368fc54087e3ad80992d04181af7da7b97d331f3cc71aa1fdc"
TIMINGS = ("client_create_ms", "exchange_ms", "validation_ms", "close_ms", "elapsed_ms")
UNKNOWN = ("server_requests", "trace_complete", "shared_server_timeline_complete",
           "client_status_matches_server", "successful_database_trace_count",
           "successful_database_traces_complete", "database_driver_version",
           "database_execute_includes_driver_lock_wait_network_and_result_receive",
           "database_fetch_includes_driver_decode_row_factory_and_python_materialization",
           "database_transaction_exit_overlaps_commit_and_close",
           "database_borrowed_transaction_context_exit_without_close",
           "pure_sql_execution_or_exact_database_lock_wait_measured", "timings_overlap_do_not_sum",
           "gc_attribution_not_exclusive")


class EvidenceError(ValueError):
    """Only hardcoded check labels may be printed by the CLI."""


def require(condition, label):
    if not condition:
        raise EvidenceError(label)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def ordered(*values):
    # Reconstructing end = start + duration can round one ULP past a boundary.
    # This is float arithmetic tolerance, not a request budget extension.
    return all(left <= math.nextafter(right, math.inf) for left, right in zip(values, values[1:]))


def same_json(left, right):
    # Mirrored artifacts must also preserve types (True must not equal 1).
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def read(path):
    def invalid_constant(_):
        raise ValueError("nonfinite JSON constant")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON field")
            result[key] = value
        return result

    raw = path.read_bytes()
    return json.loads(raw, parse_constant=invalid_constant, object_pairs_hook=unique_pairs), hashlib.sha256(raw).hexdigest()


def check_server(rows, client):
    require(type(rows) is list and len(rows) == 26, "server sample count")
    require(all(type(r) is dict and type(r.get("sample")) is int for r in rows), "server row types")
    require({r["sample"] for r in rows} == set(range(26)), "server sample identity")
    by_sample = {r["sample"]: r for r in client}
    for row in rows:
        require(type(row.get("status_code")) is int
                and row["status_code"] == by_sample[row["sample"]]["status_code"], "server response match")
        require(finite(row.get("asgi_start_offset_seconds")) and finite(row.get("wall_seconds"))
                and "error_type" in row and row["error_type"] is None, "server request timing")
        require(type(row.get("stages")) is list and bool(row["stages"]), "server stages")
        for stage in row["stages"]:
            require(type(stage) is dict and type(stage.get("stage")) is str
                    and "error_type" in stage and finite(stage.get("start_seconds"))
                    and finite(stage.get("wall_seconds")), "server stage timing")
            end = stage["start_seconds"] + stage["wall_seconds"]
            require(finite(end) and ordered(end, row["wall_seconds"]), "stage outside ASGI")
            cpu = stage.get("thread_cpu_seconds")
            require(cpu is None or finite(cpu), "server CPU timing")
        if row["status_code"] == 504:
            require(_timeout_trace_complete(row), "timeout trace incomplete")
        else:
            require(all(stage["error_type"] is None for stage in row["stages"]), "successful stage error")
            require(_fresh_database_trace_complete(row), "successful database trace incomplete")
            for label in ("recommendation_workflow", "cpu_queue_wait", "cpu_work", "execution_lease_close",
                          "execution_lease_close_database_close", "response_start_send", "response_body_complete_send"):
                require(sum(stage["stage"] == label for stage in row["stages"]) == 1, "successful boundary missing")
            require(not any(stage["stage"] == "deadline_callback" for stage in row["stages"]), "success deadline callback")
            def span(label):
                stage = next(s for s in row["stages"] if s["stage"] == label)
                return stage["start_seconds"], stage["start_seconds"] + stage["wall_seconds"]

            workflow, queue, cpu = (span(label) for label in ("recommendation_workflow", "cpu_queue_wait", "cpu_work"))
            close, driver = (span(label) for label in ("execution_lease_close", "execution_lease_close_database_close"))
            response, body = (span(label) for label in ("response_start_send", "response_body_complete_send"))
            require(ordered(workflow[0], queue[0], queue[1], cpu[0], cpu[1], close[0],
                            driver[0], driver[1], close[1], workflow[1], response[0],
                            response[1], body[0]), "successful boundary order")


def check_arm(directory, commit, hashes, trace):
    """Raw structure and recorded lifecycle checks; live resource checks are separate."""
    require(type(trace) is bool and re.fullmatch(r"[0-9a-f]{40}", commit) is not None, "expected source")
    names = ("profile", "observations", "owned", "stopped", "process-1/ready", "process-1/stopped")
    documents, fingerprints = {}, {}
    for name in names:
        documents[name], fingerprints[name + ".json"] = read(directory / (name + ".json"))
        require(type(documents[name]) is dict, "document type")
    report, obs = documents["profile"], documents["observations"]
    owned, stopped = documents["owned"], documents["stopped"]
    ready, child_stop = documents["process-1/ready"], documents["process-1/stopped"]
    require(report.get("source_commit") == commit == obs.get("source_commit")
            and report.get("source_sha256") == hashes and bool(hashes), "source fingerprint")
    for doc in (report, ready):
        require(doc.get("bundle_id") == BUNDLE and doc.get("manifest_sha256") == MANIFEST
                and doc.get("model_version") == MODEL and type(doc.get("item_count")) is int
                and doc["item_count"] == 137249 and doc.get("api_deadline_seconds") == 2.0
                and type(doc.get("api_deadline_seconds")) in (int, float), "model or deadline identity")
        require(doc.get("gc_events_enabled") is False and doc.get("phase_gate_enabled") is False, "observation settings")
    require(type(report.get("concurrency")) is int and report["concurrency"] == 2
            and type(report.get("automatic_retries")) is int and report["automatic_retries"] == 0, "load settings")
    for field, expected in (("trace_enabled", trace), ("server_instrumentation_enabled", trace),
                            ("database_driver_operations_traced", trace), ("client_phase_timing_enabled", True),
                            ("client_observations_complete", True), ("owned_schema_removed", True),
                            ("client_observation_overhead_not_subtracted", True),
                            ("instrumentation_overhead_not_subtracted", True),
                            ("diagnostic_intervention_changes_scheduling", False),
                            ("production_acceptance", False), ("sla_proven", False)):
        require(report.get(field) is expected, "report setting")
    prefix = "instrumented" if trace else "uninstrumented"
    require(report.get("status") == prefix + "_diagnostic_completed_not_performance_acceptance"
            and obs.get("status") == prefix + "_observations_only"
            and obs.get("owned_schema_removed") is True, "diagnostic status")
    require(ready.get("backend") == "numpy" and ready.get("admin_enabled") is False
            and ready.get("ephemeral") is True and owned.get("ephemeral") is True, "owned settings")
    require(type(ready.get("profile_samples", 0)) is int
            and ready.get("profile_samples", 0) == (26 if trace else 0), "trace sample setting")
    run_id = owned.get("run_id")
    require(type(run_id) is str and str(UUID(run_id)) == run_id
            and all(doc.get("run_id") == run_id for doc in (ready, stopped, child_stop)), "lifecycle identity")
    schema = owned.get("schema")
    require(type(schema) is str and re.fullmatch(r"test_evorec_[0-9a-f]{32}", schema) is not None
            and ready.get("schema") == schema, "owned schema identity")
    processes = stopped.get("processes")
    require(stopped.get("owned_schema_removed") is True and child_stop.get("cpu_jobs_drained") is True
            and type(processes) is list and len(processes) == 1 and type(processes[0]) is dict
            and type(processes[0].get("attempt")) is int and processes[0]["attempt"] == 1
            and processes[0].get("hard_killed") is False
            and processes[0].get("normal_cpu_drain") is True, "recorded cleanup")
    require(type(ready.get("pid")) is int and ready["pid"] > 0 and type(ready.get("url")) is str, "owned process identity")
    url = urlsplit(ready["url"])
    require(url.scheme == "http" and url.hostname == "127.0.0.1" and url.port not in (None, 0, 8000)
            and url.username is None and url.password is None and not (url.path or url.query or url.fragment), "owned URL")
    client = report.get("requests")
    require(type(client) is list and len(client) == 26 and same_json(obs.get("requests"), client), "client sample count")
    require(all(type(r) is dict and type(r.get("sample")) is int for r in client), "client row types")
    require({r["sample"] for r in client} == set(range(26)), "client sample identity")
    for row in client:
        require(row.get("phase") == ("sequential" if row["sample"] < 2 else "load"), "client phase")
        code = row.get("status_code")
        require(type(code) is int and code in (200, 504) and row.get("transport_error") is not True, "client status")
        require("response_identity_valid" in row and row["response_identity_valid"] is (True if code == 200 else None), "response identity")
        require(all(finite(row.get(field)) for field in TIMINGS), "client timing")
        require(sum(row[field] for field in TIMINGS[:-1]) <= row["elapsed_ms"] + .01, "client timing sum")
    successes = sum(r["status_code"] == 200 for r in client)
    require("successful_identities_valid" in report
            and report["successful_identities_valid"] is (True if successes else None), "identity coverage")
    load = [r for r in client if r["sample"] >= 2]
    require(type(report.get("load")) is dict and finite(report["load"].get("wall_seconds"))
            and report["load"]["wall_seconds"] > 0, "load duration")
    require(all(type(report["load"].get(field)) is int for field in ("requests", "successful", "failures"))
            and type(report["load"].get("status_counts")) is dict
            and all(type(value) is int for value in report["load"]["status_counts"].values()), "load count types")
    require(report["load"] == summarize(load, report["load"]["wall_seconds"]), "load summary mismatch")
    if trace:
        child, fingerprints["process-1/profile.json"] = read(directory / "process-1/profile.json")
        require(type(child) is dict and same_json(child.get("requests"), report.get("server_requests"))
                and child.get("gc_events_enabled") is False and child.get("phase_gate_enabled") is False
                and child.get("production_acceptance") is False, "child trace match")
        check_server(report.get("server_requests"), client)
        timeouts = 26 - successes
        for field in ("trace_complete", "shared_server_timeline_complete", "client_status_matches_server"):
            require(report.get(field) is True, "trace coverage flag")
        for field, count in (("successful_database_trace_count", successes), ("timeout_trace_count", timeouts)):
            require(type(report.get(field)) is int and report[field] == count, "trace count")
        for field, count in (("successful_database_traces_complete", successes), ("timeout_traces_complete", timeouts)):
            require(field in report and report[field] is (True if count else None), "trace nonvacuous coverage")
        require(type(report.get("database_driver_version")) is str
                and child.get("database_driver_version") == report["database_driver_version"], "driver version")
    else:
        require(all(field in report and report[field] is None for field in UNKNOWN), "unobserved server fields")
        require(report.get("timeout_trace_count") is None and report.get("timeout_traces_complete") is None
                and not (directory / "process-1/profile.json").exists(), "untraced child evidence")
    return dict(trace_enabled=trace, run_id=run_id, schema=schema, pid=ready["pid"], port=url.port,
                source_commit=commit, raw_sha256=fingerprints, load=report["load"],
                sequential_statuses=[r["status_code"] for r in sorted(client, key=lambda r: r["sample"])[:2]],
                failed_samples=[r["sample"] for r in client if r["status_code"] != 200],
                client_timings={field: percentiles([r[field] for r in load]) for field in TIMINGS},
                asgi_timings=(percentiles([r["wall_seconds"] * 1000 for r in report["server_requests"]
                                          if r["sample"] >= 2]) if trace else None),
                recorded_lifecycle_valid=True, live_resource_checks_performed=False,
                zero_failures=successes == 26, production_acceptance=False, sla_proven=False)


def check_series(root, commit, hashes):
    arms = [dict(arm=name, **check_arm(root / name, commit, hashes, trace)) for name, trace in ORDER]
    require(len({a["run_id"] for a in arms}) == 4 and len({a["schema"] for a in arms}) == 4, "reused ownership")
    return dict(status="diagnostic_artifacts_checked_not_performance_acceptance", source_commit=commit,
                order=[name for name, _ in ORDER], arms=arms,
                zero_failures=all(a["zero_failures"] for a in arms),
                live_resource_checks_performed=False, production_acceptance=False, sla_proven=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--arm", choices=[name for name, _ in ORDER])
    parser.add_argument("--save", action="store_true", help="write a new series-check marker, never overwrite")
    args = parser.parse_args(argv)
    try:
        require(not (args.arm and args.save), "save requires complete series")
        hashes = subprocess_sources(Path(__file__).resolve().parents[1])
        if args.arm:
            trace = dict(ORDER)[args.arm]
            result = dict(arm=args.arm, **check_arm(args.root / args.arm, args.source_commit, hashes, trace))
        else:
            result = check_series(args.root, args.source_commit, hashes)
        if args.save:
            project = Path(__file__).resolve().parents[1]
            root = args.root.resolve(strict=True)
            require(root.is_relative_to(project / "artifacts") and root != project / "artifacts", "save boundary")
            marker(root, "series-check", result)
    except (ValueError, TypeError, KeyError, OSError, OverflowError) as error:
        print(json.dumps(dict(status="diagnostic_artifacts_rejected",
                              failed_check=str(error) if type(error) is EvidenceError else type(error).__name__)))
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
