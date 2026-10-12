"""CLI boundaries, real read-only PostgreSQL and content TCP; not R06 load."""
from argparse import Namespace
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from uuid import uuid4

import psycopg
import httpx
import pytest

from scripts import demo
from scripts import run_r06_demo as old_r06

ROOT = Path(__file__).resolve().parents[1]
REAL_PACKAGES = demo.check_packages
REAL_DATABASE = demo.check_database


def arguments(**changes):
    values = dict(mode="catalog", output=None, port=0, env_file=None,
                  managed_root=None, bundle_id=None, expected_manifest_sha256=None)
    return Namespace(**(values | changes))


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(demo, "PROJECT", project)
    monkeypatch.setattr(old_r06, "__file__", str(project / "scripts/run_r06_demo.py"))
    for key in demo.KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EVOREC_DATABASE_URL", "host=127.0.0.1 dbname=test password=PRIVATE")
    monkeypatch.setattr(demo, "check_packages", lambda mode: {"synthetic": "1"})
    monkeypatch.setattr(demo, "check_database", lambda _: {"read_only": True})
    return project


def test_configuration_is_bounded_literal_and_allowlisted(tmp_path):
    path = tmp_path / "config"
    path.write_text("\ufeff# ignored\nEVOREC_DATABASE_URL = 'host=localhost password=$(PRIVATE)#literal'\n"
                    "EVOREC_ADMIN_TOKEN=PRIVATE_ADMIN\nexport EVOREC_DEMO_BUNDLE_ID=ignored\n", encoding="utf-8")
    assert demo.read_configuration(path) == {"EVOREC_DATABASE_URL": "host=localhost password=$(PRIVATE)#literal"}
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("content,code", [
    ("EVOREC_DATABASE_URL=a\nEVOREC_DATABASE_URL=b", "configuration_duplicate_key"),
    ("EVOREC_DATABASE_URL='PRIVATE", "configuration_unclosed_quote"),
    ("#" * (demo.CONFIG_LIMIT + 1), "configuration_too_large"),
])
def test_invalid_configuration_rejected(tmp_path, content, code):
    path = tmp_path / "config"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(demo.PreflightFailure) as error:
        demo.read_configuration(path)
    assert error.value.code == code and "PRIVATE" not in str(error.value)


def test_configuration_link_metadata_rejected(tmp_path, monkeypatch):
    path = tmp_path / "config"
    path.write_text("EVOREC_DATABASE_URL=PRIVATE", encoding="utf-8")
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == path)
    with pytest.raises(demo.PreflightFailure, match="configuration_link_rejected"):
        demo.read_configuration(path)


def test_explicit_configuration_wins_without_mutating_environment(isolated_project, monkeypatch):
    path = isolated_project / "config"
    path.write_text('EVOREC_DATABASE_URL="host=localhost dbname=FILE password=PRIVATE_FILE"', encoding="utf-8")
    before = os.environ.copy()
    result = demo.settings(arguments(env_file=path))
    assert "dbname=FILE" in result.database_url and result.configuration_source == "explicit_env_file"
    assert "PRIVATE" not in repr(result)
    assert os.environ == before


def test_process_precedes_implicit_file_and_file_is_fallback(isolated_project, monkeypatch):
    (isolated_project / ".env").write_text("EVOREC_DATABASE_URL=host=localhost dbname=FILE", encoding="utf-8")
    assert demo.settings(arguments()).configuration_source == "process"
    monkeypatch.delenv("EVOREC_DATABASE_URL")
    value = demo.settings(arguments())
    assert value.configuration_source == "project_env_file" and "dbname=FILE" in value.database_url


def test_empty_explicit_database_does_not_fall_back(isolated_project):
    path = isolated_project / "config"
    path.write_text("EVOREC_DATABASE_URL=", encoding="utf-8")
    with pytest.raises(demo.PreflightFailure, match="database_not_configured"):
        demo.settings(arguments(env_file=path))


@pytest.mark.parametrize("kind", ["missing", "invalid_utf8", "duplicate", "quote", "large"])
def test_bad_explicit_file_cli_sanitized(isolated_project, capsys, kind):
    path = isolated_project / "config"
    content = {"invalid_utf8": b"\xffPRIVATE", "duplicate": b"EVOREC_DATABASE_URL=PRIVATE\nEVOREC_DATABASE_URL=b",
               "quote": b"EVOREC_DATABASE_URL='PRIVATE", "large": b"P" * (demo.CONFIG_LIMIT + 1)}
    if kind != "missing":
        path.write_bytes(content[kind])
    assert demo.main(["catalog", "--env-file", str(path), "--check"]) == 1
    raw = capsys.readouterr().out
    assert "PRIVATE" not in raw and json.loads(raw)["stage"] == "configuration"


def test_missing_database_cli_sanitized(isolated_project, monkeypatch, capsys):
    monkeypatch.delenv("EVOREC_DATABASE_URL")
    assert demo.main(["catalog", "--check"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "database_not_configured"


@pytest.mark.parametrize("port", [8000, -1, 65536])
def test_bad_port_never_queries_database(isolated_project, monkeypatch, capsys, port):
    monkeypatch.setattr(demo, "check_database", lambda _: pytest.fail("DB must not be reached"))
    assert demo.main(["catalog", "--check", "--port", str(port)]) == 1
    assert json.loads(capsys.readouterr().out)["stage"] == "preflight"
    assert not (isolated_project / "artifacts").exists()


@pytest.mark.parametrize("kind", ["outside_project", "artifacts_root", "existing"])
def test_output_boundary_rejected_before_database(isolated_project, monkeypatch, capsys, kind):
    path = {"outside_project": isolated_project.parent / "outside",
            "artifacts_root": isolated_project / "artifacts",
            "existing": isolated_project / "artifacts/existing"}[kind]
    if kind == "existing":
        path.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(demo, "check_database", lambda _: calls.append(True) or {"read_only": True})
    assert demo.main(["catalog", str(path), "--check"]) == 1
    assert json.loads(capsys.readouterr().out)["stage"] == "preflight"
    assert not calls
    if kind != "existing":
        assert not path.exists()


@pytest.mark.parametrize("url", ["host=PRIVATE.invalid", "host=localhost hostaddr=203.0.113.1 password=PRIVATE", "dbname=PRIVATE"])
def test_remote_or_implicit_database_rejected(isolated_project, monkeypatch, capsys, url):
    monkeypatch.setenv("EVOREC_DATABASE_URL", url)
    monkeypatch.setattr(demo, "check_database", lambda _: pytest.fail("remote DB reached"))
    assert demo.main(["catalog", "--check"]) == 1
    assert "PRIVATE" not in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["hostaddr_env", "service_env", "service_parameter"])
def test_inherited_routing_rejected_before_database(isolated_project, monkeypatch, capsys, kind):
    if kind == "hostaddr_env":
        monkeypatch.setenv("PGHOSTADDR", "203.0.113.1")
    elif kind == "service_env":
        monkeypatch.setenv("PGSERVICE", "PRIVATE_SERVICE")
    else:
        monkeypatch.setenv("EVOREC_DATABASE_URL", "host=127.0.0.1 service=PRIVATE_SERVICE")
    calls = []
    monkeypatch.setattr(demo, "check_database", lambda _: calls.append(True) or {"read_only": True})
    assert demo.main(["catalog", "--check"]) == 1
    assert not calls
    assert "PRIVATE" not in capsys.readouterr().out


@pytest.mark.parametrize("explicit", [False, True])
def test_loopback_hostaddr_and_explicit_precedence(isolated_project, monkeypatch, capsys, explicit):
    monkeypatch.setenv("PGHOSTADDR", "203.0.113.1" if explicit else "::1")
    if explicit:
        monkeypatch.setenv("EVOREC_DATABASE_URL", "host=127.0.0.1 hostaddr=127.0.0.1")
    assert demo.main(["catalog", "--check"]) == 0
    assert json.loads(capsys.readouterr().out)["runtime_started"] is False


def test_check_only_does_not_dispatch_create_or_mutate(isolated_project, monkeypatch, capsys):
    monkeypatch.setattr(demo, "dispatch", lambda _: pytest.fail("check started runtime"))
    before = os.environ.copy()
    assert demo.main(["catalog", "--check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "preflight_passed" and report["model"] is None
    assert report["runtime_started"] is report["database_objects_created"] is report["acceptance"] is False
    assert not Path(report["output"]).exists() and "PRIVATE" not in json.dumps(report)
    assert os.environ == before


def test_default_outputs_unique(isolated_project):
    a, b = demo.settings(arguments()), demo.settings(arguments())
    assert a.output != b.output and a.output.parent == isolated_project / "artifacts/demo"
    assert not a.output.exists() and not b.output.exists()


@pytest.mark.parametrize("change", [dict(), dict(managed_root="PRIVATE", bundle_id="invalid", expected_manifest_sha256="a"*64),
                                    dict(managed_root="PRIVATE", bundle_id=str(uuid4()), expected_manifest_sha256="PRIVATE")])
def test_r06_bad_approval_sanitized(isolated_project, capsys, change):
    argv = ["r06", "--check"]
    for flag, value in change.items():
        argv.extend(["--" + flag.replace("_", "-"), value])
    assert demo.main(argv) == 1
    assert "PRIVATE" not in capsys.readouterr().out


def test_cli_model_arguments_override_configuration(isolated_project, monkeypatch):
    for key, value in zip(demo.KEYS[1:], ["old", str(uuid4()), "b"*64]):
        monkeypatch.setenv(key, value)
    identity = uuid4()
    result = demo.settings(arguments(mode="r06", managed_root="new", bundle_id=str(identity), expected_manifest_sha256="a"*64))
    assert result.approval == demo.Approval(isolated_project / "new", identity, "a"*64)
    with pytest.raises(demo.PreflightFailure, match="catalog_rejects_r06_arguments"):
        demo.settings(arguments(managed_root="new"))


@pytest.mark.parametrize("outcome", ["success", "failure", "exception", "interrupt"])
@pytest.mark.parametrize("mode", ["catalog", "r06"])
def test_dispatch_sanitizes_and_restores_environment(isolated_project, monkeypatch, outcome, mode):
    approval = demo.Approval(isolated_project / "model", uuid4(), "a"*64) if mode == "r06" else None
    value = demo.Settings(mode, isolated_project / "artifacts/run", 0, "host=localhost dbname=NEW", "process", approval)
    monkeypatch.setenv("EVOREC_ADMIN_TOKEN", "PRIVATE_BUSINESS")
    monkeypatch.setenv("EVOREC_R06_SERVING_ENABLED", "PRIVATE_FLAG")
    before = os.environ.copy()
    seen = []
    def entry(argv):
        seen.append(argv)
        assert {k:v for k,v in os.environ.items() if k.startswith("EVOREC_")} == {demo.KEYS[0]: value.database_url}
        os.environ["EVOREC_ADMIN_TOKEN"] = "owned-temporary"
        if outcome == "exception":
            raise RuntimeError("PRIVATE")
        if outcome == "interrupt":
            raise KeyboardInterrupt
        return 0 if outcome == "success" else 1
    target = importlib_demo(mode)
    monkeypatch.setattr(target, "main", entry)
    if outcome in {"exception", "interrupt"}:
        with pytest.raises(RuntimeError if outcome == "exception" else KeyboardInterrupt):
            demo.dispatch(value)
    else:
        assert demo.dispatch(value) == (0 if outcome == "success" else 1)
    assert len(seen) == 1 and os.environ == before
    if mode == "r06":
        assert seen[0][seen[0].index("--backend") + 1] == "numpy"


def importlib_demo(mode):
    from importlib import import_module
    return import_module("scripts.run_catalog_demo" if mode == "catalog" else "scripts.run_r06_demo")


@pytest.mark.parametrize("row,code", [(("off", 180000, True), "database_read_only_required"),
                                      (("on", 150000, True), "postgresql_16_required"),
                                      (("on", 180000, False), "database_create_permission_required")])
def test_database_probe_rejects_capability_mismatch(monkeypatch, row, code):
    calls = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): calls.append("closed")
        def execute(self, query):
            assert query.startswith("SELECT ")
            calls.append(query)
            return self
        def fetchone(self): return row
    def connect(url, **kwargs):
        assert kwargs["connect_timeout"] == 5
        assert "default_transaction_read_only=on" in kwargs["options"]
        assert "statement_timeout=3000" in kwargs["options"] and "lock_timeout=1000" in kwargs["options"]
        assert "sslmode" not in kwargs and "gssencmode" not in kwargs
        return Connection()
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(demo.PreflightFailure, match=code):
        REAL_DATABASE("host=localhost password=PRIVATE")
    assert len(calls) == 2 and calls[-1] == "closed"


def test_dependency_missing_and_python_version_fail_closed(monkeypatch):
    monkeypatch.setattr(demo, "package_versions", lambda profile: ({}, ["fastapi"] if profile == "service" else []))
    with pytest.raises(demo.PreflightFailure) as error:
        REAL_PACKAGES("catalog")
    assert error.value.code == "dependencies_missing" and error.value.missing == ("fastapi",)
    monkeypatch.setattr(demo.sys, "version_info", (3, 11))
    with pytest.raises(demo.PreflightFailure, match="python_312_required"):
        REAL_PACKAGES("catalog")


def test_environment_source_guard_rejects_other_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "PROJECT", tmp_path)
    with pytest.raises(demo.PreflightFailure, match="evorec_source_mismatch"):
        REAL_PACKAGES("catalog")


def test_current_import_source_matches_checkout():
    assert demo.check_source() is None


@pytest.mark.parametrize("interrupted", [False, True])
def test_main_delegate_failure_sanitized_and_environment_restored(isolated_project, monkeypatch, capsys, interrupted):
    before = os.environ.copy()
    def failed(_):
        if interrupted:
            raise KeyboardInterrupt
        raise RuntimeError("PRIVATE")
    monkeypatch.setattr(importlib_demo("catalog"), "main", failed)
    assert demo.main(["catalog"]) == (130 if interrupted else 1)
    raw = capsys.readouterr().out
    assert "PRIVATE" not in raw and os.environ == before
    if not interrupted:
        assert json.loads(raw.splitlines()[-1])["stage"] == "run"


def test_database_error_cli_does_not_leak_message(isolated_project, monkeypatch, capsys):
    def failed(_): raise psycopg.OperationalError("host=PRIVATE password=PRIVATE")
    monkeypatch.setattr(demo, "check_database", failed)
    assert demo.main(["catalog", "--check"]) == 1
    raw = capsys.readouterr().out
    assert "PRIVATE" not in raw and json.loads(raw)["error_type"] == "OperationalError"


def test_read_only_database_probe_creates_no_namespace(isolated_database, monkeypatch, capsys):
    monkeypatch.setenv("EVOREC_DATABASE_URL", isolated_database)
    def names():
        with psycopg.connect(isolated_database) as connection:
            return connection.execute("SELECT nspname FROM pg_namespace ORDER BY nspname").fetchall()
    before = names()
    assert demo.main(["catalog", "--check"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["database"]["read_only"] is True and result["database"]["schema_create_permission"] is True
    assert not Path(result["output"]).exists() and names() == before


def test_real_catalog_cli_tcp_and_normal_stop(isolated_database):
    output = ROOT / "artifacts/test-demo-cli" / uuid4().hex
    environment = {k:v for k,v in os.environ.items() if not k.startswith("EVOREC_")}
    environment.update(EVOREC_DATABASE_URL=isolated_database, EVOREC_ADMIN_TOKEN="PRIVATE_BUSINESS",
                       EVOREC_R06_SERVING_ENABLED="1", EVOREC_DEMO_BUNDLE_ID="PRIVATE_UNUSED")
    child = subprocess.Popen([sys.executable, "-m", "scripts.demo", "catalog", str(output)],
        cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    ready = None
    try:
        deadline = monotonic() + 40
        while not (output / "ready.json").exists():
            assert child.poll() is None, "new CLI exited before ready"
            assert monotonic() < deadline, "new CLI readiness deadline"
            sleep(.05)
        ready = json.loads((output / "ready.json").read_bytes())
        token = (output / "admin-token.txt").read_text()
        assert ready["ephemeral"] and ready["item_count"] == 24 and ready["model_kind"] == "content-baseline"
        assert token != environment["EVOREC_ADMIN_TOKEN"] and len(token) >= 32
        with httpx.Client(base_url=ready["url"], trust_env=False, timeout=10) as client:
            from scripts.verify_catalog_demo import exercise
            checks = exercise(client, ready, token, output)
            assert checks["cold_item_recommendable_only_after_publish"]
            assert checks["original_recommendation_replays_after_rollback"]
    finally:
        if child.poll() is None:
            if output.is_dir(): (output / "stop").touch()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
                pytest.fail("force kill; cleanup not proven")
        log = child.stdout.read().decode("utf-8", errors="replace")
        child.stdout.close()
    assert child.returncode == 0, log
    assert "PRIVATE" not in log and token not in log
    precheck = json.loads(log.splitlines()[0])
    assert precheck["status"] == "preflight_passed" and precheck["acceptance"] is False
    stopped = json.loads((output / "stopped.json").read_bytes())
    assert stopped["worker_drained"] and stopped["owned_schema_removed"]
    assert stopped["schema"] == ready["schema"] and stopped["run_id"] == ready["run_id"]
    assert not (output / "admin-token.txt").exists()
    with psycopg.connect(isolated_database) as connection:
        assert connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (ready["schema"],)).fetchone() is None
