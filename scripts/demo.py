"""One local Demo entry: bounded configuration, read-only checks, old lifecycle."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import sys
from uuid import UUID, uuid4

from scripts.check_environment import package_versions

PROJECT = Path(__file__).resolve().parents[1]
CONFIG_LIMIT = 16 * 1024
NUMPY_VERSION = "2.1.3"
KEYS = ("EVOREC_DATABASE_URL", "EVOREC_DEMO_MANAGED_ROOT",
        "EVOREC_DEMO_BUNDLE_ID", "EVOREC_DEMO_MANIFEST_SHA256")


class PreflightFailure(Exception):
    def __init__(self, code, *, missing=()):
        super().__init__(code)
        self.code, self.missing = code, tuple(missing)


@dataclass(frozen=True)
class Approval:
    root: Path
    bundle_id: UUID
    digest: str


@dataclass(frozen=True)
class Settings:
    mode: str
    output: Path
    port: int
    database_url: str = field(repr=False)
    configuration_source: str
    approval: Approval | None = None


def local_path(path):
    value = Path(path)
    return value if value.is_absolute() else PROJECT / value


def read_configuration(path):
    """Literal assignments only; never evaluate the file or include its values."""
    path = local_path(path)
    if path.is_symlink() or path.is_junction():
        raise PreflightFailure("configuration_link_rejected")
    with path.open("rb") as stream:
        raw = stream.read(CONFIG_LIMIT + 1)
    if len(raw) > CONFIG_LIMIT:
        raise PreflightFailure("configuration_too_large")
    result = {}
    for line in raw.decode("utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in KEYS:
            continue
        if key in result:
            raise PreflightFailure("configuration_duplicate_key")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise PreflightFailure("configuration_unclosed_quote")
            value = value[1:-1]
        result[key] = value
    return result


def settings(args):
    values = {key: os.environ[key] for key in KEYS if key in os.environ}
    source = "process"
    if args.env_file is not None:
        values.update(read_configuration(args.env_file))
        source = "explicit_env_file"
    elif not values.get(KEYS[0]) and (PROJECT / ".env").exists():
        values.update(read_configuration(PROJECT / ".env"))
        source = "project_env_file"
    database_url = values.get(KEYS[0], "")
    if not database_url:
        raise PreflightFailure("database_not_configured")
    output = local_path(args.output) if args.output else PROJECT / "artifacts/demo" / f"{args.mode}-{uuid4().hex}"
    approval = None
    if args.mode == "catalog" and any((args.managed_root, args.bundle_id, args.expected_manifest_sha256)):
        raise PreflightFailure("catalog_rejects_r06_arguments")
    if args.mode == "r06":
        root = args.managed_root or values.get(KEYS[1])
        identity = args.bundle_id or values.get(KEYS[2])
        digest = args.expected_manifest_sha256 or values.get(KEYS[3])
        if not root or not identity or not digest:
            raise PreflightFailure("r06_approval_required")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PreflightFailure("r06_approval_invalid")
        approval = Approval(local_path(root), UUID(identity), digest)
    return Settings(args.mode, output, args.port, database_url, source, approval)


def check_packages(mode):
    if sys.version_info[:2] != (3, 12):
        raise PreflightFailure("python_312_required")
    installed, missing = {}, []
    for profile in ("service", "database"):
        found, absent = package_versions(profile)
        installed.update(found)
        missing.extend(absent)
    if mode == "r06":
        try:
            installed["numpy"] = version("numpy")
            numpy = importlib.import_module("numpy")
        except ImportError:
            missing.append("numpy")
        else:
            if installed["numpy"] != NUMPY_VERSION or numpy.__version__ != NUMPY_VERSION:
                raise PreflightFailure("numpy_version_unsupported")
    if missing:
        raise PreflightFailure("dependencies_missing", missing=missing)
    check_source()
    return installed


def check_source():
    # Editable environments can still refer to another worktree. Do not silently
    # run that backend or reinstall/change sys.path on the user's behalf.
    import evorec
    if Path(evorec.__file__).resolve() != (PROJECT / "src/evorec/__init__.py").resolve():
        raise PreflightFailure("evorec_source_mismatch")


def check_database(database_url):
    import psycopg
    # Diagnostic-only connection; keep authentication/SSL policy, override only
    # its read-only and timeout settings. Never migrate or create an object.
    with psycopg.connect(database_url, connect_timeout=5, application_name="evorec-demo-preflight",
                         options="-c default_transaction_read_only=on -c statement_timeout=3000 -c lock_timeout=1000") as connection:
        row = connection.execute(
            "SELECT current_setting('transaction_read_only'), "
            "current_setting('server_version_num')::integer, "
            "has_database_privilege(current_database(), 'CREATE')").fetchone()
    if row[0] != "on":
        raise PreflightFailure("database_read_only_required")
    if row[1] < 160000:
        raise PreflightFailure("postgresql_16_required")
    if row[2] is not True:
        raise PreflightFailure("database_create_permission_required")
    return dict(read_only=True, server_version_num=row[1], schema_create_permission=True)


def preflight(configuration):
    if configuration.mode == "r06" and configuration.approval is None:
        raise PreflightFailure("r06_approval_required")
    installed = check_packages(configuration.mode)
    from scripts.run_r06_demo import validate_isolation
    # Keep the original boundary before any model/DB access; no mkdir/bind here.
    output, _ = validate_isolation(configuration.output, configuration.database_url, configuration.port)
    model = None
    if configuration.approval is not None:
        from evorec.infrastructure.r06_bundle import load_r06_bundle
        approval = configuration.approval
        bundle = load_r06_bundle(approval.root, approval.root / str(approval.bundle_id),
            expected_manifest_sha256=approval.digest, content_backend="numpy", ranker_backend="numpy")
        if bundle.bundle_id != approval.bundle_id or bundle.manifest_sha256 != approval.digest:
            raise PreflightFailure("r06_identity_mismatch")
        model = dict(bundle_id=str(bundle.bundle_id), manifest_sha256=bundle.manifest_sha256,
                     item_count=len(bundle.catalog_item_sha256), backend="numpy")
    database = check_database(configuration.database_url)
    return dict(status="preflight_passed", mode=configuration.mode, output=str(output),
        configuration_source=configuration.configuration_source, packages=installed,
        import_source_verified=True, executable=sys.executable,
        database=database, model=model, ephemeral=True, runtime_started=False,
        database_objects_created=False, acceptance=False)


@contextmanager
def demo_environment(database_url):
    """CLI-only scope. Reuse the original in-process Ctrl+C/finally lifecycle."""
    saved = {key: value for key, value in os.environ.items() if key.startswith("EVOREC_")}
    try:
        for key in list(saved):
            os.environ.pop(key)
        os.environ[KEYS[0]] = database_url
        yield
    finally:
        for key in list(os.environ):
            if key.startswith("EVOREC_"):
                os.environ.pop(key)
        os.environ.update(saved)


def dispatch(configuration):
    if configuration.mode == "catalog":
        from scripts.run_catalog_demo import main as entry
        argv = [str(configuration.output), "--port", str(configuration.port)]
    else:
        from scripts.run_r06_demo import main as entry
        approval = configuration.approval
        argv = [str(configuration.output), str(approval.root), str(approval.bundle_id),
                "--expected-manifest-sha256", approval.digest, "--backend", "numpy",
                "--port", str(configuration.port)]
    with demo_environment(configuration.database_url):
        return entry(argv)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("catalog", "r06"))
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--check", action="store_true", help="read-only preflight; no API/schema/output created")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--managed-root")
    parser.add_argument("--bundle-id")
    parser.add_argument("--expected-manifest-sha256")
    args = parser.parse_args(argv)
    stage = "configuration"
    try:
        configuration = settings(args)
        stage = "preflight"
        report = preflight(configuration)
        print(json.dumps(report, sort_keys=True), flush=True)
        if args.check:
            return 0
        stage = "run"
        return dispatch(configuration)
    except KeyboardInterrupt:
        return 130  # Before readiness, or an unexpected delegate interruption.
    except Exception as error:
        report = dict(status="failed", stage=stage, code=f"{stage}_failed", error_type=type(error).__name__)
        if isinstance(error, PreflightFailure):
            report["code"] = error.code
            if error.missing:
                report["missing"] = list(error.missing)
        print(json.dumps(report, sort_keys=True), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
