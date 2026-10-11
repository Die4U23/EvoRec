"""Independent fixed-series evidence gates, including all-failure and forged green controls."""

from copy import deepcopy
import json
import math
from pathlib import Path
from uuid import uuid4

import pytest

from scripts import check_r06_trace_abba as checker
from scripts.verify_r06_reliability import summarize
from test_r06_timeout_trace import complete_timeout, event


COMMIT = "a" * 40
HASHES = {"src/example.py": "b" * 64, "scripts/profile_r06_tcp.py": "c" * 64}
SUCCESS_STAGES = (
    "publication_recovery_database_connect", "execution_lease_and_admission_database_connect",
    "execution_lease_and_admission_database_execute_execution_lock",
    "database_admission_database_execute_session_snapshot_lock",
    "database_admission_database_execute_accepted_insert",
    "database_admission_database_transaction_context_exit",
    "actual_catalog_read_and_capture_database_execute_actual_catalog_rows",
    "actual_catalog_read_and_capture_database_fetchone_decode", "result_write_database_connect",
    "result_write_database_commit", "result_write_database_close", "execution_lease_close_database_close",
    "recommendation_workflow", "cpu_queue_wait", "cpu_work", "execution_lease_close",
    "response_start_send", "response_body_complete_send",
)


def documents(trace, statuses=None):
    statuses = statuses if statuses is not None else [200] * 26
    run_id, schema = str(uuid4()), "test_evorec_" + uuid4().hex
    rows = [dict(sample=i, phase="sequential" if i < 2 else "load", status_code=code,
                 response_identity_valid=True if code == 200 else None,
                 client_create_ms=1.0, exchange_ms=7.0, validation_ms=1.0, close_ms=1.0, elapsed_ms=10.0)
            for i, code in enumerate(statuses)]
    success = statuses.count(200)
    fixed = dict(bundle_id=checker.BUNDLE, manifest_sha256=checker.MANIFEST, model_version=checker.MODEL,
                 item_count=137249, api_deadline_seconds=2.0, gc_events_enabled=False, phase_gate_enabled=False)
    prefix = "instrumented" if trace else "uninstrumented"
    report = dict(**fixed, source_commit=COMMIT, source_sha256=HASHES, concurrency=2, automatic_retries=0,
                  trace_enabled=trace, server_instrumentation_enabled=trace, database_driver_operations_traced=trace,
                  client_phase_timing_enabled=True, client_observations_complete=True, owned_schema_removed=True,
                  client_observation_overhead_not_subtracted=True, instrumentation_overhead_not_subtracted=True,
                  diagnostic_intervention_changes_scheduling=False, production_acceptance=False, sla_proven=False,
                  status=prefix + "_diagnostic_completed_not_performance_acceptance", requests=rows,
                  successful_identities_valid=True if success else None, load=summarize(rows[2:], 2.0))
    ready = dict(**fixed, backend="numpy", admin_enabled=False, ephemeral=True, run_id=run_id,
                 schema=schema, pid=1234, url="http://127.0.0.1:54321", profile_samples=26 if trace else 0)
    result = dict(profile=report, observations=dict(source_commit=COMMIT, requests=deepcopy(rows),
                  status=prefix + "_observations_only", owned_schema_removed=True),
                  owned=dict(run_id=run_id, schema=schema, ephemeral=True),
                  stopped=dict(run_id=run_id, owned_schema_removed=True,
                               processes=[dict(attempt=1, hard_killed=False, normal_cpu_drain=True)]),
                  **{"process-1/ready": ready, "process-1/stopped": dict(run_id=run_id, cpu_jobs_drained=True)})
    if trace:
        server = []
        for index, code in enumerate(statuses):
            row = (dict(status_code=200, error_type=None, wall_seconds=1.0,
                        stages=[event(label, .01) for label in SUCCESS_STAGES])
                   if code == 200 else complete_timeout())
            if code == 200:
                spans = {"recommendation_workflow": (0, .8), "cpu_queue_wait": (.4, .03),
                         "cpu_work": (.43, .1), "execution_lease_close": (.65, .02),
                         "execution_lease_close_database_close": (.651, .001),
                         "response_start_send": (.82, .01), "response_body_complete_send": (.84, .01)}
                for stage in row["stages"]:
                    if stage["stage"] in spans:
                        stage["start_seconds"], stage["wall_seconds"] = spans[stage["stage"]]
            row.update(sample=index, asgi_start_offset_seconds=float(index))
            server.append(row)
        report.update(server_requests=server, trace_complete=True, shared_server_timeline_complete=True,
                      client_status_matches_server=True, successful_database_trace_count=success,
                      successful_database_traces_complete=True if success else None,
                      timeout_trace_count=26-success, timeout_traces_complete=True if success < 26 else None,
                      database_driver_version="3.3.6")
        result["process-1/profile"] = dict(requests=deepcopy(server), gc_events_enabled=False,
            phase_gate_enabled=False, production_acceptance=False, database_driver_version="3.3.6")
    else:
        report.update({field: None for field in checker.UNKNOWN})
    return result


def write(directory, docs):
    for name, data in docs.items():
        path = directory / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("statuses", [[200]*26, [504]*26, [200, 504]*13])
def test_raw_arms_include_all_failures_and_keep_zero_coverage_unknown(tmp_path, trace, statuses):
    write(tmp_path, documents(trace, statuses))
    result = checker.check_arm(tmp_path, COMMIT, HASHES, trace)
    assert result["load"]["requests"] == 24
    assert result["load"]["failures"] == statuses[2:].count(504)
    assert result["zero_failures"] is (504 not in statuses)
    assert result["production_acceptance"] is False and result["sla_proven"] is False
    assert result["live_resource_checks_performed"] is False
    if 200 not in statuses:
        assert all(value is None for value in result["load"]["successful_latency"].values())
    if not trace:
        assert result["asgi_timings"] is None


@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("defect", [
    "missing_sample", "duplicate_sample", "bool_sample", "phase", "unexpected_status", "transport",
    "identity", "negative_timing", "nan_timing", "bool_timing", "timing_sum", "source", "hash",
    "manifest", "model", "catalog_size", "deadline", "concurrency", "retry", "gc", "phase_gate",
    "summary_failure", "summary_rate", "status_count", "trace_setting", "profile_samples",
    "server_claim", "cleanup", "cpu_not_drained", "hard_killed", "bool_attempt", "owner",
    "schema", "admin", "url_port", "url_remote", "pid", "overhead", "acceptance", "sla",
])
def test_rejects_forged_green_or_changed_protocol(tmp_path, trace, defect):
    docs = documents(trace)
    report, ready = docs["profile"], docs["process-1/ready"]
    row = report["requests"][2]
    if defect == "missing_sample": report["requests"].pop()
    elif defect == "duplicate_sample": row["sample"] = 0
    elif defect == "bool_sample": row["sample"] = True
    elif defect == "phase": row["phase"] = "sequential"
    elif defect == "unexpected_status": row["status_code"] = 500
    elif defect == "transport": row["transport_error"] = True
    elif defect == "identity": row["response_identity_valid"] = False
    elif defect.endswith("timing"):
        row["exchange_ms"] = {"negative_timing": -1, "nan_timing": float("nan"), "bool_timing": True}[defect]
    elif defect == "timing_sum": row["exchange_ms"] = 100
    elif defect == "source": report["source_commit"] = "d" * 40
    elif defect == "hash": report["source_sha256"] = {"src/example.py": "d" * 64}
    elif defect == "manifest": report["manifest_sha256"] = "d" * 64
    elif defect == "model": ready["model_version"] = "changed"
    elif defect == "catalog_size": report["item_count"] = 100
    elif defect == "deadline": ready["api_deadline_seconds"] = 3.0
    elif defect == "concurrency": report["concurrency"] = 1
    elif defect == "retry": report["automatic_retries"] = 1
    elif defect == "gc": report["gc_events_enabled"] = True
    elif defect == "phase_gate": ready["phase_gate_enabled"] = True
    elif defect == "summary_failure": report["load"]["failures"] = 1
    elif defect == "summary_rate": report["load"]["success_rate"] = .5
    elif defect == "status_count": report["load"]["status_counts"] = {"200": 23, "504": 1}
    elif defect == "trace_setting": report["trace_enabled"] = not trace
    elif defect == "profile_samples": ready["profile_samples"] = True
    elif defect == "server_claim": report["shared_server_timeline_complete"] = not trace
    elif defect == "cleanup": docs["stopped"]["owned_schema_removed"] = False
    elif defect == "cpu_not_drained": docs["process-1/stopped"]["cpu_jobs_drained"] = False
    elif defect == "hard_killed": docs["stopped"]["processes"][0]["hard_killed"] = True
    elif defect == "bool_attempt": docs["stopped"]["processes"][0]["attempt"] = True
    elif defect == "owner": ready["run_id"] = str(uuid4())
    elif defect == "schema": ready["schema"] = "public"
    elif defect == "admin": ready["admin_enabled"] = True
    elif defect == "url_port": ready["url"] = "http://127.0.0.1:8000"
    elif defect == "url_remote": ready["url"] = "https://example.com:54321"
    elif defect == "pid": ready["pid"] = True
    elif defect == "overhead": report["instrumentation_overhead_not_subtracted"] = False
    elif defect == "acceptance": report["production_acceptance"] = True
    elif defect == "sla": report["sla_proven"] = True
    else: pytest.fail("unknown test defect")
    docs["observations"]["requests"] = deepcopy(report["requests"])
    write(tmp_path, docs)
    with pytest.raises(ValueError): checker.check_arm(tmp_path, COMMIT, HASHES, trace)


@pytest.mark.parametrize("defect", [
    "missing_row", "duplicate_row", "status", "offset", "stage_end", "stage_cpu", "missing_success",
    "missing_timeout", "hidden_timeout", "forged_coverage", "vacuous_timeout", "vacuous_success", "child",
    "close_before_cpu", "send_before_exit", "bool_trace_count",
])
def test_trace_recomputes_actual_success_and_failure_coverage(tmp_path, defect):
    docs = documents(True, [200] * 25 + [504])
    report = docs["profile"]
    server = report["server_requests"]
    if defect == "missing_row": server.pop()
    elif defect == "duplicate_row": server[2]["sample"] = 1
    elif defect == "status": server[2]["status_code"] = 504
    elif defect == "offset": server[2]["asgi_start_offset_seconds"] = -1
    elif defect == "stage_end": server[2]["stages"][0]["wall_seconds"] = 20
    elif defect == "stage_cpu": server[2]["stages"][0]["thread_cpu_seconds"] = float("inf")
    elif defect == "missing_success": server[2]["stages"] = server[2]["stages"][1:]
    elif defect == "missing_timeout": server[-1]["stages"] = [e for e in server[-1]["stages"] if e["stage"] != "deadline_callback"]
    elif defect == "hidden_timeout": report["timeout_trace_count"] = 0
    elif defect == "forged_coverage": report["timeout_traces_complete"] = False
    elif defect == "vacuous_timeout": docs = documents(True); docs["profile"]["timeout_traces_complete"] = True
    elif defect == "vacuous_success": docs = documents(True, [504]*26); docs["profile"]["successful_database_traces_complete"] = True
    elif defect == "child": docs["process-1/profile"]["requests"] = []
    elif defect == "close_before_cpu":
        next(e for e in server[2]["stages"] if e["stage"] == "cpu_work")["wall_seconds"] = .3
    elif defect == "send_before_exit":
        next(e for e in server[2]["stages"] if e["stage"] == "recommendation_workflow")["wall_seconds"] = .9
    elif defect == "bool_trace_count": report["timeout_trace_count"] = True
    if defect not in ("child", "vacuous_timeout", "vacuous_success"):
        docs["process-1/profile"]["requests"] = deepcopy(server)
    write(tmp_path, docs)
    with pytest.raises(ValueError): checker.check_arm(tmp_path, COMMIT, HASHES, True)


def test_fixed_series_all_failure_is_complete_not_stable_and_reused_ownership_rejected(tmp_path):
    for arm, trace in checker.ORDER:
        write(tmp_path / arm, documents(trace, [504]*26))
    report = checker.check_series(tmp_path, COMMIT, HASHES)
    assert report["order"] == ["a1", "b1", "b2", "a2"]
    assert [a["load"]["failures"] for a in report["arms"]] == [24]*4
    assert report["zero_failures"] is False
    assert report["production_acceptance"] is False
    first = json.loads((tmp_path / "a1/owned.json").read_bytes())
    for path in ("owned", "stopped", "process-1/ready", "process-1/stopped"):
        file = tmp_path / "a2" / (path + ".json")
        data = json.loads(file.read_bytes())
        data["run_id"] = first["run_id"]
        file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="reused ownership"): checker.check_series(tmp_path, COMMIT, HASHES)


@pytest.mark.parametrize("contents", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_rejects_duplicate_or_nonfinite_json_fields(tmp_path, contents):
    path = tmp_path / "report.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError): checker.read(path)


def test_reconstructed_boundaries_tolerate_only_one_ulp_not_real_inversion():
    assert .4 + .03 > .43
    assert checker.ordered(.4 + .03, .43)
    assert checker.ordered(.43, .43, .44)
    assert not checker.ordered(math.nextafter(math.nextafter(.43, math.inf), math.inf), .43)
    assert not checker.ordered(.43 + 1e-9, .43)


@pytest.mark.parametrize("trace", [False, True])
def test_mirrored_observations_reject_bool_int_coercion(tmp_path, trace):
    docs = documents(trace)
    docs["observations"]["requests"][1]["sample"] = True
    write(tmp_path, docs)
    with pytest.raises(ValueError, match="client sample count"):
        checker.check_arm(tmp_path, COMMIT, HASHES, trace)


def test_cli_partial_is_not_series_and_rejection_does_not_leak_payload(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(checker, "subprocess_sources", lambda _: HASHES)
    write(tmp_path / "a1", documents(False, [504]*26))
    args = [str(tmp_path), "--source-commit", COMMIT]
    assert checker.main([*args, "--arm", "a1"]) == 0
    assert json.loads(capsys.readouterr().out)["zero_failures"] is False
    assert checker.main(args) == 1
    assert checker.main([*args, "--arm", "a1", "--save"]) == 1
    (tmp_path / "a1/profile.json").write_text("PRIVATE-DSN-TOKEN", encoding="utf-8")
    assert checker.main([*args, "--arm", "a1"]) == 1
    assert "PRIVATE" not in capsys.readouterr().out


def test_cli_save_uses_fresh_marker_only_in_artifacts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(checker, "subprocess_sources", lambda _: HASHES)
    project = Path(checker.__file__).resolve().parents[1]
    root = project / "artifacts" / "test-trace-abba" / uuid4().hex
    for arm, trace in checker.ORDER:
        write(root / arm, documents(trace))
    args = [str(root), "--source-commit", COMMIT, "--save"]
    assert checker.main(args) == 0
    saved = (root / "series-check.json").read_bytes()
    assert checker.main(args) == 1
    assert (root / "series-check.json").read_bytes() == saved
    assert json.loads(saved)["production_acceptance"] is False
    for arm, trace in checker.ORDER:
        write(tmp_path / arm, documents(trace))
    assert checker.main([str(tmp_path), "--source-commit", COMMIT, "--save"]) == 1
    assert not (tmp_path / "series-check.json").exists()
    assert "PRIVATE" not in capsys.readouterr().out
