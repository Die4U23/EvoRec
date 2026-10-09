"""Explicit no-server-trace control for the bounded R06 TCP diagnostic."""

import json
from uuid import UUID, uuid4

import httpx
import pytest

from scripts import profile_r06_tcp as profiler
from scripts.r06_service_lab import R06ServiceLab
from test_r06_bundle import _build


def cli_args(tmp_path):
    return [str(tmp_path / "run"), str(tmp_path / "bundles"), str(uuid4()),
            "--expected-manifest-sha256", "b" * 64, "--samples", "2", "--concurrency", "1"]


def client_rows(statuses, identities=None):
    identities = identities or [True if status == 200 else None for status in statuses]
    rows = []
    for index, status in enumerate(statuses):
        row = dict(sample=index, phase="sequential" if index < 2 else "load",
                   status_code=status, response_identity_valid=identities[index], elapsed_ms=10.0,
                   client_create_ms=1.0, exchange_ms=7.0, validation_ms=1.0, close_ms=1.0)
        if status is None:
            row["transport_error"] = True
        rows.append(row)
    return rows


def valid_untraced_report(statuses=(504, 504, 504, 504), identities=None):
    rows = client_rows(list(statuses), identities)
    successful = [row for row in rows if row["status_code"] == 200]
    return dict(
        status="uninstrumented_diagnostic_completed_not_performance_acceptance",
        trace_enabled=False,
        server_instrumentation_enabled=False,
        client_phase_timing_enabled=True,
        client_observation_overhead_not_subtracted=True,
        gc_events_enabled=False,
        phase_gate_enabled=False,
        client_observations_complete=True,
        requests=rows,
        load=dict(requests=2, successful=sum(r["status_code"] == 200 for r in rows[2:]),
                  failures=sum(r["status_code"] != 200 for r in rows[2:]),
                  status_counts={"200": sum(r["status_code"] == 200 for r in rows[2:]),
                                 "504": sum(r["status_code"] == 504 for r in rows[2:])}),
        source_commit="a" * 40,
        source_sha256={"scripts/profile_r06_tcp.py": "c" * 64},
        bundle_id=str(uuid4()), manifest_sha256="b" * 64,
        model_version="approved-model", item_count=137249, concurrency=1,
        api_deadline_seconds=2.0, automatic_retries=0,
        owned_schema_removed=True, cleanup_complete=True,
        successful_identities_valid=(all(r["response_identity_valid"] is True for r in successful)
                                     if successful else None),
        server_requests=None, trace_complete=None, shared_server_timeline_complete=None,
        client_status_matches_server=None, successful_database_trace_count=None,
        successful_database_traces_complete=None, database_driver_operations_traced=False,
        database_driver_version=None,
        database_execute_includes_driver_lock_wait_network_and_result_receive=None,
        database_fetch_includes_driver_decode_row_factory_and_python_materialization=None,
        database_transaction_exit_overlaps_commit_and_close=None,
        pure_sql_execution_or_exact_database_lock_wait_measured=None,
        timings_overlap_do_not_sum=None, instrumentation_overhead_not_subtracted=True,
        gc_attribution_not_exclusive=None,
        diagnostic_intervention_changes_scheduling=False,
        production_acceptance=False, sla_proven=False)


def test_trace_argument_requires_exact_bool_and_rejects_before_source(tmp_path, monkeypatch):
    def source_must_not_run(*args):
        pytest.fail("invalid trace control reached source or database work")
    monkeypatch.setattr(profiler, "_source", source_must_not_run)
    monkeypatch.setattr(profiler, "subprocess_sources", source_must_not_run)
    monkeypatch.setattr(profiler, "R06ServiceLab", source_must_not_run)
    for bad in (0, 1, None, "false"):
        with pytest.raises(ValueError, match="trace"):
            profiler.profile(tmp_path / str(bad), "PRIVATE-DB", tmp_path, uuid4(), "b" * 64,
                             trace=bad)


@pytest.mark.parametrize("kwargs", [
    {"gc_events": True}, {"phase_gate": True}, {"gc_events": 1}, {"phase_gate": 0},
])
def test_untraced_profile_rejects_instrumentation_options_before_source(tmp_path, monkeypatch, kwargs):
    def source_must_not_run(*args):
        pytest.fail("incompatible untraced controls reached source or database work")
    monkeypatch.setattr(profiler, "_source", source_must_not_run)
    monkeypatch.setattr(profiler, "subprocess_sources", source_must_not_run)
    monkeypatch.setattr(profiler, "R06ServiceLab", source_must_not_run)
    with pytest.raises(ValueError):
        profiler.profile(tmp_path / "rejected", "PRIVATE-DB", tmp_path, uuid4(), "b" * 64,
                         trace=False, **kwargs)


def test_profile_defaults_to_tracing_and_cli_untraced_flags_are_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE-DB")
    calls = []

    def fake_profile(*args, **kwargs):
        calls.append(kwargs)
        if kwargs["trace"]:
            return dict(status="instrumented_diagnostic_completed_not_performance_acceptance",
                        trace_complete=True, client_status_matches_server=True,
                        shared_server_timeline_complete=True, successful_database_trace_count=0,
                        successful_database_traces_complete=None, successful_identities_valid=None)
        return valid_untraced_report()

    monkeypatch.setattr(profiler, "profile", fake_profile)
    args = cli_args(tmp_path)
    assert profiler.main(args) == 0
    assert profiler.main([*args, "--untraced", "--no-gc-events"]) == 0
    assert calls[0]["trace"] is True
    assert calls[0]["gc_events"] is True and calls[0]["phase_gate"] is False
    assert calls[1]["trace"] is False
    assert calls[1]["gc_events"] is False and calls[1]["phase_gate"] is False


def test_http_transport_error_keeps_null_status_and_explicit_marker(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **kwargs): pass
        def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise httpx.ConnectError("PRIVATE-ENDPOINT")
        def close(self): pass

    monkeypatch.setattr(profiler.httpx, "Client", Client)
    monkeypatch.setattr(profiler, "legal", lambda *args: pytest.fail("no HTTP response to validate"))
    record = profiler.sample("http://127.0.0.1:1", {},
                             dict(access_token="PRIVATE", session_id="session", history_version=0),
                             0, "load")
    assert len(calls) == 1
    assert record["status_code"] is None
    assert record["transport_error"] is True
    assert record["response_identity_valid"] is None


@pytest.mark.parametrize("statuses,identities,expected_identity,stale_child,sample_change,complete", [
    ((504, 504, 504, 504), None, None, False, None, True),
    ((200, 504, 200, 504), None, True, True, None, True),
    ((200, 504, 504, 504), (False, None, None, None), False, False, None, True),
    ((504, 504, 504, 504), None, None, False, "duplicate", False),
    ((504, 504, 504, 504), None, None, False, "missing", False),
    ((504, 504, 504, 504), None, None, False, "bool", False),
])
def test_untraced_profile_keeps_client_evidence_without_reading_child_profile(
        monkeypatch, tmp_path, statuses, identities, expected_identity, stale_child, sample_change, complete):
    from scripts.run_r06_demo import marker

    monkeypatch.setattr(profiler, "__file__", str(tmp_path / "scripts" / "profile_r06_tcp.py"))
    monkeypatch.setattr(profiler, "_source", lambda _: "a" * 40)
    monkeypatch.setattr(profiler, "subprocess_sources", lambda _: {"src.py": "c" * 64})
    created_labs = []

    class Lab:
        def __init__(self, output, *args, profile_samples, gc_events, phase_gate):
            assert profile_samples == 0
            assert gc_events is False and phase_gate is False
            self.output, self.child_output = output, output / "child"
            self.created = True
            self.ready = dict(url="http://127.0.0.1:43210", model_version="approved-model",
                              item_count=137249, profile_samples=0,
                              gc_events_enabled=False, phase_gate_enabled=False)
            created_labs.append(self)

        def __enter__(self):
            self.child_output.mkdir(parents=True)
            marker(self.child_output, "ready", self.ready)
            if stale_child:
                (self.child_output / "profile.json").write_text("stale child trace", encoding="utf-8")
            return self

        def __exit__(self, *_):
            self.created = False

    class Response:
        status_code = 201
        def json(self):
            return dict(access_token="PRIVATE", session_id="session", history_version=0)

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def post(self, *args, **kwargs): return Response()

    monkeypatch.setattr(profiler, "R06ServiceLab", Lab)
    monkeypatch.setattr(profiler.httpx, "Client", Client)
    rows = client_rows(list(statuses), identities)
    if sample_change == "duplicate":
        rows[3]["sample"] = 2
    elif sample_change == "missing":
        rows[3].pop("sample")
    elif sample_change == "bool":
        rows[3]["sample"] = True
    monkeypatch.setattr(profiler, "sample", lambda url, ready, session, index, phase: rows[index])
    output = tmp_path / "artifacts" / "untraced"
    report = profiler.profile(output, "PRIVATE-DB", tmp_path, uuid4(), "b" * 64,
                              samples=2, concurrency=1, trace=False,
                              gc_events=False, phase_gate=False)

    assert len(created_labs) == 1 and created_labs[0].created is False
    assert created_labs[0].ready["profile_samples"] == 0
    assert created_labs[0].ready["gc_events_enabled"] is False
    assert created_labs[0].ready["phase_gate_enabled"] is False
    assert (created_labs[0].child_output / "profile.json").exists() is stale_child
    assert report["trace_enabled"] is False
    assert report["server_instrumentation_enabled"] is False
    assert report["client_phase_timing_enabled"] is True
    assert report["status"] == "uninstrumented_diagnostic_completed_not_performance_acceptance"
    assert report["client_observations_complete"] is complete
    assert len(report["requests"]) == 4
    if complete:
        assert {row["sample"] for row in report["requests"]} == set(range(4))
    assert [row["status_code"] for row in report["requests"]] == list(statuses)
    for key in ("server_requests", "trace_complete", "shared_server_timeline_complete",
                "client_status_matches_server", "successful_database_trace_count",
                "successful_database_traces_complete"):
        assert report[key] is None
    assert report["database_driver_operations_traced"] is False
    assert report["successful_identities_valid"] is expected_identity
    assert report["production_acceptance"] is False and report["sla_proven"] is False
    assert report["source_commit"] == "a" * 40 and report["source_sha256"] == {"src.py": "c" * 64}
    assert report["model_version"] == "approved-model" and report["item_count"] == 137249
    assert report["owned_schema_removed"] is True
    assert json.loads((output / "profile.json").read_text())["requests"] == rows


@pytest.mark.parametrize("ready_change", [
    "profile_samples_positive", "missing_gc_events", "gc_events_wrong_type",
    "missing_phase_gate", "phase_gate_wrong_type",
])
def test_untraced_profile_refuses_instrumented_or_unverifiable_ready(
        monkeypatch, tmp_path, ready_change):
    from scripts.run_r06_demo import marker

    monkeypatch.setattr(profiler, "__file__", str(tmp_path / "scripts" / "profile_r06_tcp.py"))
    monkeypatch.setattr(profiler, "_source", lambda _: "a" * 40)
    monkeypatch.setattr(profiler, "subprocess_sources", lambda _: {"src.py": "c" * 64})

    class Lab:
        def __init__(self, output, *args, profile_samples, gc_events, phase_gate):
            assert profile_samples == 0 and gc_events is False and phase_gate is False
            self.output, self.child_output, self.created = output, output / "child", True
            self.ready = dict(url="http://127.0.0.1:43210", model_version="approved-model",
                              item_count=137249, profile_samples=0,
                              gc_events_enabled=False, phase_gate_enabled=False)
            if ready_change == "profile_samples_positive":
                self.ready["profile_samples"] = 2
            elif ready_change == "missing_gc_events":
                self.ready.pop("gc_events_enabled")
            elif ready_change == "gc_events_wrong_type":
                self.ready["gc_events_enabled"] = 0
            elif ready_change == "missing_phase_gate":
                self.ready.pop("phase_gate_enabled")
            else:
                self.ready["phase_gate_enabled"] = 0

        def __enter__(self):
            self.child_output.mkdir(parents=True)
            marker(self.child_output, "ready", self.ready)
            return self

        def __exit__(self, *_):
            self.created = False

    monkeypatch.setattr(profiler, "R06ServiceLab", Lab)
    monkeypatch.setattr(profiler.httpx, "Client", lambda **kwargs:
                        pytest.fail("bad child ready metadata reached a client request"))
    monkeypatch.setattr(profiler, "sample", lambda *args: pytest.fail("bad child ready metadata reached sampling"))
    with pytest.raises(ValueError, match="owned API untraced settings changed"):
        profiler.profile(tmp_path / ready_change, "PRIVATE-DB", tmp_path, uuid4(), "b" * 64,
                         samples=2, concurrency=1, trace=False, gc_events=False, phase_gate=False)


def test_untraced_profile_rejects_source_change_without_final_report(monkeypatch, tmp_path):
    from scripts.run_r06_demo import marker

    monkeypatch.setattr(profiler, "__file__", str(tmp_path / "scripts" / "profile_r06_tcp.py"))
    sources = iter(("a" * 40, "d" * 40))
    monkeypatch.setattr(profiler, "_source", lambda _: next(sources))
    monkeypatch.setattr(profiler, "subprocess_sources", lambda _: {"src.py": "c" * 64})

    class Lab:
        def __init__(self, output, *args, profile_samples, gc_events, phase_gate):
            assert profile_samples == 0 and gc_events is False and phase_gate is False
            self.output, self.child_output, self.created = output, output / "child", True
            self.ready = dict(url="http://127.0.0.1:43210", model_version="approved-model",
                              item_count=137249, profile_samples=0,
                              gc_events_enabled=False, phase_gate_enabled=False)

        def __enter__(self):
            self.child_output.mkdir(parents=True)
            marker(self.child_output, "ready", self.ready)
            return self

        def __exit__(self, *_):
            self.created = False

    class Response:
        status_code = 201
        def json(self):
            return dict(access_token="PRIVATE", session_id="session", history_version=0)

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def post(self, *args, **kwargs): return Response()

    monkeypatch.setattr(profiler, "R06ServiceLab", Lab)
    monkeypatch.setattr(profiler.httpx, "Client", Client)
    monkeypatch.setattr(profiler, "sample", lambda url, ready, session, index, phase:
                        client_rows([504] * 4)[index])
    output = tmp_path / "source-changed"
    with pytest.raises(ValueError, match="source changed"):
        profiler.profile(output, "PRIVATE-DB", tmp_path, uuid4(), "b" * 64,
                         samples=2, concurrency=1, trace=False, gc_events=False, phase_gate=False)
    assert (output / "observations.json").exists()
    assert not (output / "profile.json").exists()


def test_real_untraced_synthetic_tcp_uses_zero_samples_without_child_trace(isolated_database, tmp_path):
    root, target, digest = _build(tmp_path)
    output = tmp_path / "real-untraced"
    lab = R06ServiceLab(output, isolated_database, root, UUID(target.name), digest, "stdlib",
                        profile_samples=0, gc_events=False, phase_gate=False)
    with lab:
        # Existing untraced child omits this optional key; positive sampling is
        # always advertised. Preserve that production metadata contract.
        assert type(lab.ready.get("profile_samples", 0)) is int
        assert lab.ready.get("profile_samples", 0) == 0
        assert lab.ready["gc_events_enabled"] is False
        assert lab.ready["phase_gate_enabled"] is False
        with httpx.Client(base_url=lab.ready["url"], timeout=10, trust_env=False) as client:
            created = client.post("/api/v1/sessions", json={"profile_id": "sample"})
            assert created.status_code == 201
            session = created.json()
            for _ in range(2):
                response = client.post("/api/v1/recommendations", headers={
                    "X-Session-Token": session["access_token"],
                    "Idempotency-Key": str(uuid4()),
                }, json={"session_id": session["session_id"],
                         "expected_history_version": session["history_version"],
                         "strategy": "dense", "k": 2})
                assert response.status_code == 200
                body = response.json()
                assert body["bundle_id"] == lab.ready["bundle_id"]
                assert body["model_version"] == lab.ready["model_version"]
                assert body["session_id"] == session["session_id"]
                assert body["actual_strategy"] == "dense" and body["fallback_reason"] is None
                assert len(body["items"]) == 2
                assert len({item["item_id"] for item in body["items"]}) == 2
                assert all(item["source"] == "r06-a-frozen-s17"
                           and item["item_id"] not in session["history"] for item in body["items"])
    assert not lab.created and lab.stopped[-1]["normal_cpu_drain"] is True
    stopped = json.loads((output / "stopped.json").read_text(encoding="utf-8"))
    assert stopped["owned_schema_removed"] is True
    assert not (lab.child_output / "profile.json").exists()


@pytest.mark.parametrize("mutation", [
    "trace_true", "server_requests_empty", "trace_count_zero", "coverage_empty",
    "status_match_true", "identity_false", "identity_true_without_success",
    "identity_row_false_summary_true", "missing_200_identity", "wrong_200_status_type",
    "client_incomplete", "duplicate_index",
    "missing_client_row", "gc_flag_true", "phase_flag_wrong_type", "cleanup_false",
    "acceptance_true", "sla_true", "driver_trace_true", "missing_server_field",
    "server_instrumentation_wrong_type", "client_timing_false",
])
def test_cli_rejects_inconsistent_or_forged_untraced_report(monkeypatch, tmp_path, mutation):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE-DB")
    report = valid_untraced_report()
    monkeypatch.setattr(profiler, "profile", lambda *args, **kwargs: report)
    args = [*cli_args(tmp_path), "--untraced", "--no-gc-events"]
    assert profiler.main(args) == 0
    if mutation == "trace_true":
        report["trace_enabled"] = True
    elif mutation == "server_requests_empty":
        report["server_requests"] = []
    elif mutation == "trace_count_zero":
        report["successful_database_trace_count"] = 0
    elif mutation == "coverage_empty":
        report["successful_database_traces_complete"] = []
    elif mutation == "status_match_true":
        report["client_status_matches_server"] = True
    elif mutation == "identity_false":
        report["successful_identities_valid"] = False
    elif mutation == "identity_true_without_success":
        report["successful_identities_valid"] = True
    elif mutation == "identity_row_false_summary_true":
        report["requests"][0].update(status_code=200, response_identity_valid=False)
        report["successful_identities_valid"] = True
    elif mutation == "client_incomplete":
        report["client_observations_complete"] = 1
    elif mutation == "duplicate_index":
        report["requests"][3]["sample"] = 2
        report["client_observations_complete"] = True
    elif mutation == "missing_client_row":
        report["requests"].pop()
        report["client_observations_complete"] = True
    elif mutation == "missing_200_identity":
        report["requests"][0].update(status_code=200)
        report["successful_identities_valid"] = True
        report["requests"][0].pop("response_identity_valid")
    elif mutation == "wrong_200_status_type":
        report["requests"][0].update(status_code="200", response_identity_valid=True)
        report["successful_identities_valid"] = True
    elif mutation == "gc_flag_true":
        report["gc_events_enabled"] = True
    elif mutation == "phase_flag_wrong_type":
        report["phase_gate_enabled"] = 0
    elif mutation == "cleanup_false":
        report["owned_schema_removed"] = False
        report["cleanup_complete"] = False
    elif mutation == "acceptance_true":
        report["production_acceptance"] = True
    elif mutation == "sla_true":
        report["sla_proven"] = True
    elif mutation == "driver_trace_true":
        report["database_driver_operations_traced"] = True
    elif mutation == "server_instrumentation_wrong_type":
        report["server_instrumentation_enabled"] = 0
    elif mutation == "client_timing_false":
        report["client_phase_timing_enabled"] = False
    else:
        report.pop("shared_server_timeline_complete")

    assert profiler.main(args) == 1


UNTRACED_SERVER_UNKNOWN_FIELDS = (
    "server_requests", "trace_complete", "shared_server_timeline_complete",
    "client_status_matches_server", "successful_database_trace_count",
    "successful_database_traces_complete", "database_driver_version",
    "database_execute_includes_driver_lock_wait_network_and_result_receive",
    "database_fetch_includes_driver_decode_row_factory_and_python_materialization",
    "database_transaction_exit_overlaps_commit_and_close",
    "pure_sql_execution_or_exact_database_lock_wait_measured",
    "timings_overlap_do_not_sum", "gc_attribution_not_exclusive",
)


@pytest.mark.parametrize("field", UNTRACED_SERVER_UNKNOWN_FIELDS)
@pytest.mark.parametrize("forged", [True, 0, []])
def test_cli_rejects_each_forged_server_unknown_value(monkeypatch, tmp_path, field, forged):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE-DB")
    report = valid_untraced_report()
    monkeypatch.setattr(profiler, "profile", lambda *args, **kwargs: report)
    args = [*cli_args(tmp_path), "--untraced", "--no-gc-events"]
    assert profiler.main(args) == 0
    report[field] = forged
    assert profiler.main(args) == 1


@pytest.mark.parametrize("field", UNTRACED_SERVER_UNKNOWN_FIELDS)
def test_cli_rejects_each_missing_server_unknown_field(monkeypatch, tmp_path, field):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE-DB")
    report = valid_untraced_report()
    monkeypatch.setattr(profiler, "profile", lambda *args, **kwargs: report)
    args = [*cli_args(tmp_path), "--untraced", "--no-gc-events"]
    assert profiler.main(args) == 0
    report.pop(field)
    assert profiler.main(args) == 1


@pytest.mark.parametrize("statuses,identities,expected", [
    ((504, 504, 504, 504), (None, None, None, None), 0),
    ((200, 504, 200, 504), (True, None, True, None), 0),
    ((200, 504, 504, 504), (False, None, None, None), 1),
])
def test_cli_untraced_mode_gates_only_real_client_success_identity(
        monkeypatch, tmp_path, statuses, identities, expected):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE-DB")
    valid_identities = tuple(True if status == 200 else None for status in statuses)
    report = valid_untraced_report(statuses, valid_identities)
    monkeypatch.setattr(profiler, "profile", lambda *args, **kwargs: report)
    args = [*cli_args(tmp_path), "--untraced", "--no-gc-events"]
    assert profiler.main(args) == 0
    if expected:
        report["requests"][0]["response_identity_valid"] = False
        report["successful_identities_valid"] = False
    assert profiler.main(args) == expected


def test_cli_accepts_null_status_only_with_transport_error_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE-DB")
    report = valid_untraced_report()
    report["requests"][0]["status_code"] = None
    report["requests"][0]["transport_error"] = True
    monkeypatch.setattr(profiler, "profile", lambda *args, **kwargs: report)
    args = [*cli_args(tmp_path), "--untraced", "--no-gc-events"]
    assert profiler.main(args) == 0


@pytest.mark.parametrize("bad_status,transport_marker", [
    (0, True), (None, False), (True, True), ("504", True), (None, 1),
])
def test_cli_rejects_invalid_transport_status_observations(
        monkeypatch, tmp_path, bad_status, transport_marker):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE-DB")
    report = valid_untraced_report()
    report["requests"][0]["status_code"] = None
    report["requests"][0]["transport_error"] = True
    monkeypatch.setattr(profiler, "profile", lambda *args, **kwargs: report)
    args = [*cli_args(tmp_path), "--untraced", "--no-gc-events"]
    assert profiler.main(args) == 0
    report["requests"][0]["status_code"] = bad_status
    if transport_marker:
        report["requests"][0]["transport_error"] = transport_marker
    else:
        report["requests"][0].pop("transport_error")
    assert profiler.main(args) == 1


@pytest.mark.parametrize("extra_flags", [["--untraced"], ["--untraced", "--phase-gate"]])
def test_cli_rejects_untraced_without_required_no_gc_events_or_with_phase_gate(
        monkeypatch, tmp_path, extra_flags):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE-DB")
    monkeypatch.setattr(profiler, "profile", lambda *args, **kwargs: valid_untraced_report())
    assert profiler.main([*cli_args(tmp_path), "--untraced", "--no-gc-events"]) == 0
    monkeypatch.setattr(profiler, "profile", lambda *args, **kwargs:
                        pytest.fail("invalid CLI combination reached profile/DB work"))
    assert profiler.main([*cli_args(tmp_path), *extra_flags]) == 1
