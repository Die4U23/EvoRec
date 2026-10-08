"""Connection diagnostic guards, negative evidence, and real read-only sessions."""

from collections import Counter
import ipaddress
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import profile_database_handshake as probe


URL = "host=127.0.0.1 port=5432 dbname=private user=private password=PRIVATE"


def test_metadata_query_cannot_read_shadowed_business_objects():
    assert "FROM pg_catalog.pg_stat_gssapi" in probe.METADATA
    for function in ("host", "inet_server_addr", "inet_server_port", "current_database", "current_setting", "pg_backend_pid"):
        assert f"pg_catalog.{function}(" in probe.METADATA


@pytest.mark.parametrize("value", ["host=example.org", "host=localhost", "host=127.0.0.1,127.0.0.2",
    "host=127.0.0.1 service=private", "host=127.0.0.1 servicefile=private",
    "host=127.0.0.1 hostaddr=127.0.0.2", "host=127.0.0.1 port=5432,5433",
    "host=127.0.0.1 port=0", "host=127.0.0.1 port=65536",
    "host=127.0.0.1 gssencmode=require", "host=127.0.0.1 require_auth=gss",
    "host=127.0.0.1 require_auth=sspi", "host=127.0.0.1 replication=true",
    "host=127.0.0.1 dbname='host=example.org'", "malformed PRIVATE"])
def test_unsafe_parameters_fail_before_connection(value, monkeypatch):
    monkeypatch.setattr(probe.psycopg, "connect", lambda *a, **kw: pytest.fail("no connection"))
    with pytest.raises(probe.GuardError):
        probe.parameters(value)


@pytest.mark.parametrize("name", ["PGHOST", "PGPASSWORD", "PGSERVICE", "PGSSLMODE", "PGGSSENCMODE"])
def test_inherited_parameters_refused(monkeypatch, name):
    monkeypatch.setenv(name, "PRIVATE")
    with pytest.raises(probe.GuardError, match="inherited_pg_parameters_refused"):
        probe.parameters(URL)


@pytest.mark.parametrize("ssl", ["prefer", "require", "verify-ca", "verify-full"])
@pytest.mark.parametrize("timeout, expected", [("1", "1"), ("8", "5"), ("0", "5"), ("-1", "5")])
def test_only_one_trial_factor_changes_and_ssl_credentials_options_retained(ssl, timeout, expected):
    arms = probe.parameters(URL + f" sslmode={ssl} connect_timeout={timeout} options='-c application_name=private'")
    baseline = arms["baseline"]
    for arm, factor in (("gss_disabled", "gssencmode"), ("hostaddr_explicit", "hostaddr")):
        assert {k for k in set(baseline) | set(arms[arm]) if baseline.get(k) != arms[arm].get(k)} == {factor}
        assert arms[arm]["sslmode"] == ssl and arms[arm]["password"] == "PRIVATE"
    assert baseline["connect_timeout"] == expected
    assert baseline["options"].endswith("-c default_transaction_read_only=on -c statement_timeout=2000")
    assert baseline["options"].startswith("-c application_name=private")


class Connection:
    def __init__(self, *, row=None, ssl=False, error=None):
        self.row = row if row is not None else ("127.0.0.1", 5432, "PRIVATE-db", "PRIVATE-user", "on", False, False)
        self.info = SimpleNamespace(server_version=160006)
        self.pgconn = SimpleNamespace(ssl_in_use=ssl)
        self.closed = False
        self.error = error

    def execute(self, statement):
        assert statement == probe.METADATA
        if self.error:
            raise self.error
        return self

    def fetchone(self):
        return self.row

    def close(self):
        self.closed = True


def test_observe_preserves_security_identity_and_omits_private_values(monkeypatch):
    connections = [Connection(ssl=True), Connection(ssl=True), Connection(ssl=False)]
    monkeypatch.setattr(probe.psycopg, "connect", lambda **kw: connections.pop(0))
    first, identity = probe.observe({}, "baseline", -1)
    second, _ = probe.observe({}, "hostaddr_explicit", 0, identity)
    third, _ = probe.observe({}, "gss_disabled", 0, identity)
    assert first["success"] and second["success"]
    assert not third["success"] and third["error_code"] == "connection_identity_or_ssl_changed"
    assert all(r["connection_closed"] for r in (first, second, third))
    assert "PRIVATE" not in json.dumps([first, second, third])


@pytest.mark.parametrize("row", [("192.0.2.1",5432,"db","u","on",False,False),
    ("127.0.0.1",5432,"db","u","off",False,False),
    ("127.0.0.1",5432,"db","u","on",True,False),
    ("127.0.0.1",5432,"db","u","on",False,True)])
def test_nonlocal_writable_or_gss_session_closed_and_refused(monkeypatch, row):
    connection = Connection(row=row)
    monkeypatch.setattr(probe.psycopg, "connect", lambda **kw: connection)
    record, _ = probe.observe({}, "baseline", -1)
    assert not record["success"] and record["error_code"] == "nonlocal_writable_or_gss_session_refused"
    assert connection.closed


@pytest.mark.parametrize("stage", ["connect", "metadata", "close"])
def test_failures_are_counted_sanitized_and_not_retried(monkeypatch, stage):
    connection, calls = Connection(error=RuntimeError("PRIVATE") if stage == "metadata" else None), []
    def connect(**kwargs):
        calls.append(kwargs)
        if stage == "connect": raise RuntimeError("PRIVATE")
        return connection
    if stage == "close":
        def close(): raise RuntimeError("PRIVATE")
        connection.close = close
    monkeypatch.setattr(probe.psycopg, "connect", connect)
    record, _ = probe.observe({}, "baseline", -1)
    assert not record["success"] and len(calls) == 1
    assert "PRIVATE" not in json.dumps(record)
    if stage == "metadata": assert connection.closed


def clean_source(monkeypatch):
    monkeypatch.setattr(probe, "_source", lambda *a: "a"*40)
    monkeypatch.setattr(probe, "subprocess_sources", lambda *a: {"scripts/probe.py":"b"*64})


def test_balanced_schedule_real_warmup_excluded_and_incomplete_pairs_not_zero(monkeypatch):
    # Output must live inside the project-owned artifacts tree even in unit tests.
    output = Path(__file__).resolve().parents[1] / "artifacts" / f"test-handshake-{uuid4().hex}"
    clean_source(monkeypatch)
    captured = []
    monkeypatch.setattr(probe, "marker", lambda directory, name, report: captured.append(report))
    def observe(params, arm, index, expected):
        failed = index == 2 and arm == "gss_disabled"
        return dict(arm=arm, round=index, warmup=index==-1, success=not failed,
            error_code="connection_or_metadata_error" if failed else None,
            connect_ms=1000 if index==-1 else {"baseline":10,"gss_disabled":8,"hostaddr_explicit":11}[arm],
            connect_thread_cpu_ms=1), ("internal",)
    monkeypatch.setattr(probe, "observe", observe)
    try:
        result = probe.profile(output, URL, rounds=6)
        assert not result["complete"] and result["stop_code"] is None
        assert result["summary"]["baseline"]["connect_median_ms"] == 10
        assert result["summary"]["gss_disabled"]["failures"] == 1
        assert result["paired_comparisons"]["gss_disabled"]["candidate_minus_baseline_ms"][2] is None
        assert result["paired_comparisons"]["gss_disabled"]["pairs"] == 5
        assert captured == [result] and len(result["observations"]) == 21
        for position in range(3):
            assert Counter(order[position] for order in result["scheduled_orders"][1:]) == Counter(dict.fromkeys(probe.ARMS,2))
    finally:
        output.rmdir()  # only this test's new empty directory


@pytest.mark.parametrize("reason", ["warmup", "identity", "source", "hashes", "budget"])
def test_incomplete_runs_never_pass_and_stop_safely(monkeypatch, reason):
    output = Path(__file__).resolve().parents[1] / "artifacts" / f"test-handshake-{uuid4().hex}"
    clean_source(monkeypatch)
    captured, clock = [], [0.0]
    monkeypatch.setattr(probe, "marker", lambda *a: captured.append(a[-1]))
    monkeypatch.setattr(probe, "perf_counter", lambda: clock[0])
    def observe(params, arm, index, expected):
        code = "connection_or_metadata_error" if reason=="warmup" else "connection_identity_or_ssl_changed" if reason=="identity" else None
        if reason=="budget": clock[0] += 61
        return dict(arm=arm, round=index, warmup=index==-1, success=code is None,
            error_code=code, connect_ms=1, connect_thread_cpu_ms=1), ("internal",)
    monkeypatch.setattr(probe, "observe", observe)
    if reason=="source":
        revisions = iter(["a"*40,"c"*40])
        monkeypatch.setattr(probe, "_source", lambda *a: next(revisions))
    if reason=="hashes":
        hashes = iter([{"script":"a"*64}, {"script":"c"*64}])
        monkeypatch.setattr(probe, "subprocess_sources", lambda *a: next(hashes))
    try:
        result = probe.profile(output, URL, rounds=6)
        assert not result["complete"] and not result["sla_proven"]
        if reason not in ("source", "hashes"): assert len(result["observations"]) == 1
        else: assert not result["source_unchanged"]
        assert result["summary"]["baseline"]["observed"] == (6 if reason in ("source", "hashes") else 0)
    finally:
        output.rmdir()


@pytest.mark.parametrize("rounds", [0, True, 5, 7, 25, 30])
def test_invalid_bounds_rejected_before_any_resource(monkeypatch, tmp_path, rounds):
    monkeypatch.setattr(probe, "parameters", lambda *a: pytest.fail("preflight bounds first"))
    with pytest.raises(probe.GuardError): probe.profile(tmp_path, URL, rounds=rounds)


def numeric_fixture_arms(database_url):
    from psycopg.conninfo import make_conninfo
    # CI's fixture uses localhost. This numeric-only diagnostic intentionally
    # rejects hostnames; pin only this isolated test URL to its actual endpoint.
    with probe.psycopg.connect(database_url) as connection:
        address = connection.info.hostaddr
        server_address = connection.execute("SELECT pg_catalog.host(pg_catalog.inet_server_addr())").fetchone()[0]
    return (probe.parameters(make_conninfo(database_url, host=address, hostaddr=address)),
            ipaddress.ip_address(server_address).is_loopback)


def test_real_database_connections_are_fresh_readonly_and_preserve_session_security(isolated_database, monkeypatch):
    arms, server_is_loopback = numeric_fixture_arms(isolated_database)
    connect = probe.psycopg.connect
    pids = []
    def physical_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        pids.append(connection.info.backend_pid)
        return connection
    monkeypatch.setattr(probe.psycopg, "connect", physical_connect)
    expected = None
    records = []
    for arm in probe.ARMS:
        record, identity = probe.observe(arms[arm], arm, -1, expected)
        assert record["connection_closed"], record
        if server_is_loopback:
            assert record["success"] and record["read_only"], record
        else:
            # Docker forwards a client loopback port to a nonloopback server
            # address. Prove refusal, not acceptance or a skipped real test.
            assert not record["success"]
            assert record["error_code"] == "nonlocal_writable_or_gss_session_refused"
        expected = identity
        records.append(record)
    assert "PRIVATE" not in json.dumps(records)
    assert len(set(pids)) == 3
    with probe.psycopg.connect(**arms["baseline"], autocommit=True) as connection:
        with pytest.raises(probe.psycopg.errors.ReadOnlySqlTransaction):
            connection.execute("CREATE TABLE forbidden_probe_write (n integer)")


def test_real_cast_regression_cannot_be_hidden_by_mocked_bare_ip_rows(isolated_database, monkeypatch):
    arms, _ = numeric_fixture_arms(isolated_database)
    old = probe.METADATA.replace("pg_catalog.host(pg_catalog.inet_server_addr())", "pg_catalog.inet_server_addr()::text")
    assert old != probe.METADATA
    monkeypatch.setattr(probe, "METADATA", old)
    record, _ = probe.observe(arms["baseline"], "baseline", -1)
    assert not record["success"] and record["error_code"] == "connection_or_metadata_error"
    assert record["connection_closed"]


@pytest.mark.parametrize("complete", [False, True])
def test_cli_failure_and_completeness_gate(monkeypatch, tmp_path, capsys, complete):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE")
    monkeypatch.setattr(probe, "profile", lambda *a, **kw: dict(status="test",complete=complete))
    assert probe.main([str(tmp_path)]) == (0 if complete else 1)
    assert "PRIVATE" not in capsys.readouterr().out


def test_cli_preflight_exception_message_is_not_exported(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE")
    def fail(*a, **kw): raise RuntimeError("PRIVATE")
    monkeypatch.setattr(probe, "profile", fail)
    assert probe.main([str(tmp_path)]) == 1
    assert "PRIVATE" not in capsys.readouterr().out


@pytest.mark.parametrize("target", ["outside", "root", "existing"])
def test_output_boundary_checked_before_source_and_connections(monkeypatch, tmp_path, target):
    root = Path(__file__).resolve().parents[1]
    path = tmp_path if target == "outside" else root / "artifacts" if target == "root" else root / "artifacts" / f"test-existing-{uuid4().hex}"
    if target == "existing": path.mkdir(parents=True)
    monkeypatch.setattr(probe, "_source", lambda *a: pytest.fail("output boundary first"))
    try:
        with pytest.raises(probe.GuardError, match="fresh_artifacts_subdirectory_required"):
            probe.profile(path, URL, rounds=6)
    finally:
        if target == "existing": path.rmdir()
