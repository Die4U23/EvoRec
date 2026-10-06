"""Prepare a real approved R06 corpus only in an owned temporary PostgreSQL schema."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from scripts.assemble_r06_bundle import _source
from scripts.migrate_database import migrate
from scripts.verify_r06_bundle import SOURCE_FILES as BUNDLE_SOURCES
from evorec.domain.errors import ManagementError
from evorec.infrastructure.management import CatalogManager
from evorec.infrastructure.postgres import PostgresDemoBackend

SOURCE_FILES = (*BUNDLE_SOURCES, "src/evorec/infrastructure/r06_catalog.py",
                "src/evorec/infrastructure/management.py", "src/evorec/infrastructure/postgres.py",
                "src/evorec/api/app.py", "src/evorec/bootstrap.py", "db/migrations/0009_r06_catalog_preparation.sql",
                "scripts/migrate_database.py", "scripts/verify_r06_catalog_preparation.py",
                "tests/test_r06_catalog_preparation.py", "tests/test_catalog_lock.py",
                "src/evorec/infrastructure/catalog_file_job.py", ".github/workflows/service-integration.yml")


def verify(output, database_url, managed_root, bundle_id, digest):
    project = Path(__file__).resolve().parents[1]
    output = Path(output).resolve()
    if not output.is_relative_to(project / "artifacts") or output == project / "artifacts":
        raise ValueError("verification must use a fresh artifacts subdirectory")
    if output.exists():
        raise FileExistsError("verification destination already exists")
    base = _source(project)
    schema = "test_evorec_" + uuid4().hex
    isolated = make_conninfo(**{**conninfo_to_dict(database_url), "options": "-c search_path="+schema})
    created = False
    output.mkdir(parents=True, exist_ok=False)
    report, owned = output / "verification.json", False
    try:
        hashes = {}
        for name in SOURCE_FILES:
            raw = (project / name).read_bytes()
            target = output / "source" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()
        with psycopg.connect(database_url) as c:
            c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        created = True  # Set only after CREATE committed; never drop another owner's schema.
        migrate(isolated)
        backend = PostgresDemoBackend(isolated)
        manager = CatalogManager(backend, Path(managed_root))
        before = manager.publication_state()
        prepared = manager.prepare_r06_bundle(bundle_id, digest)
        if manager.publication_state() != before or backend.runtime is not None or prepared["replayed"]:
            raise ValueError("preparation changed publication or was not a first commit")
        restarted = CatalogManager(PostgresDemoBackend(isolated), Path(managed_root))
        if not restarted.prepare_r06_bundle(bundle_id, digest)["replayed"]:
            raise ValueError("restart did not replay the approved preparation")
        with manager._connect() as c:
            # Full stored text/time/display and ordered membership revalidation.
            runtime = restarted.r06.load_registered(c, bundle_id)
            counts = [c.execute("SELECT count(*) AS n FROM "+table).fetchone()["n"]
                      for table in ("items", "bundle_versions", "bundle_items")]
            first = c.execute("SELECT item_id, r06_first_seen_ms FROM items ORDER BY item_id LIMIT 1").fetchone()
        if counts != [prepared["item_count"], 1, prepared["item_count"]]:
            raise ValueError("stored corpus or registration counts differ")
        try:
            manager.publish(uuid4(), bundle_id, None)
        except ManagementError as error:
            if error.code != "r06_online_not_enabled": raise
        else:
            raise ValueError("unservable R06 package was activated")
        with manager._connect() as c:
            c.execute("UPDATE items SET r06_first_seen_ms = r06_first_seen_ms + 1 WHERE item_id=%s", (first["item_id"],))
        try:
            restarted.prepare_r06_bundle(bundle_id, digest)
        except ManagementError as error:
            if error.code != "r06_catalog_changed": raise
        else:
            raise ValueError("real database representation drift was accepted")
        with manager._connect() as c:
            c.execute("UPDATE items SET r06_first_seen_ms=%s WHERE item_id=%s", (first["r06_first_seen_ms"], first["item_id"]))
            if c.execute("SELECT count(*) AS n FROM publication_operations").fetchone()["n"]:
                raise ValueError("rejected activation created a publication operation")
        if manager.publication_state() != before:
            raise ValueError("active state changed during preparation checks")
        if _source(project) != base or any(hashlib.sha256((project / name).read_bytes()).hexdigest() != h for name,h in hashes.items()):
            raise ValueError("source changed during verification")
        result = dict(status="passed", activated=False, business_schema_untouched=True,
                      item_count=prepared["item_count"], bundle_id=str(bundle_id), manifest_sha256=digest,
                      model_version=runtime.model_version, database_sources_exact=True, restart_replay_exact=True,
                      real_database_drift_rejected=True, unservable_publication_rejected=True,
                      source=dict(base_commit=base, working_tree_dirty=False, source_sha256=hashes))
    finally:
        if created:
            with psycopg.connect(database_url) as c:
                c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            created = False
    # Only publish a passed report after successful owned-schema cleanup.
    result["owned_temporary_schema_removed"] = True
    try:
        with report.open("xb") as stream:
            owned = True
            stream.write(json.dumps(result, sort_keys=True, allow_nan=False).encode("utf-8"))
    except BaseException:
        if owned: report.unlink(missing_ok=True)
        raise
    return result


def main(argv=None):
    from uuid import UUID
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("managed_root", type=Path)
    parser.add_argument("bundle_id", type=UUID)
    parser.add_argument("--expected-manifest-sha256", required=True)
    args = parser.parse_args(argv)
    database_url = os.getenv("EVOREC_DATABASE_URL")
    if not database_url:
        print(json.dumps(dict(status="failed", code="database_not_configured")))
        return 1
    try:
        result = verify(args.output, database_url, args.managed_root, args.bundle_id, args.expected_manifest_sha256)
    except (ValueError, OSError, psycopg.Error, subprocess.SubprocessError, ManagementError) as error:
        # Never print connection strings or raw database diagnostics.
        print(json.dumps(dict(status="failed", code=getattr(error, "code", "verification_failed"), error_type=type(error).__name__)))
        return 1
    print(json.dumps({key:value for key,value in result.items() if key != "source"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
