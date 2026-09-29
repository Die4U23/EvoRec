"""Restore both PostgreSQL state and managed bundles from one stopped-service backup."""

import asyncio
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from evorec.api.app import create_app
from evorec.bootstrap import build_demo_application
from evorec.contracts import CatalogImportInput


def _pg_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable:
        return executable
    install_root = Path(os.environ.get("ProgramFiles", "")) / "PostgreSQL"
    matches = sorted(install_root.glob(f"*/bin/{name}.exe"))
    if not matches:
        raise AssertionError(f"{name} is required for the backup/restore acceptance test")
    return str(matches[-1])


def _postgres_environment(database_url: str) -> tuple[dict[str, str], str, str]:
    connection = conninfo_to_dict(database_url)
    options = connection.get("options", "")
    match = re.fullmatch(r"-c search_path=(test_evorec_[0-9a-f]{32})", options)
    assert match, "backup test may only operate on an isolated pytest schema"
    environment = os.environ.copy()
    for field, variable in (("host", "PGHOST"), ("port", "PGPORT"),
                            ("user", "PGUSER"), ("password", "PGPASSWORD"),
                            ("dbname", "PGDATABASE")):
        if field in connection:
            environment[variable] = str(connection[field])
    assert environment.get("PGDATABASE"), "database name is required"
    return environment, environment["PGDATABASE"], match.group(1)


def _file_hashes(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def test_stopped_service_backup_restores_published_catalog(isolated_database, monkeypatch, tmp_path):
    bundle_root = tmp_path / "active-bundles"
    monkeypatch.setenv("EVOREC_BUNDLE_ROOT", str(bundle_root))
    application = build_demo_application()
    manager = application.manager
    batch_id = uuid4()
    manager.import_items(CatalogImportInput(batch_id=batch_id, items=[
        {"item_id": "restored-item", "title": "Restored puzzle", "category": "puzzle"},
    ]))
    build = manager.builds.process(batch_id, uuid4())
    manager.builds.publish(build["build_id"], uuid4())
    expected_bundle = build["bundle_id"]
    assert manager.publication_state()["admission_open"] is True

    backup_file = tmp_path / "catalog.dump"
    backup_bundles = tmp_path / "backup-bundles"
    environment, database_name, schema = _postgres_environment(isolated_database)
    subprocess.run(
        [_pg_tool("pg_dump"), "--format=custom", "--no-owner", "--no-privileges",
         f"--schema={schema}", f"--file={backup_file}"],
        env=environment, check=True, capture_output=True, text=True, timeout=30,
    )
    shutil.copytree(bundle_root, backup_bundles)
    assert backup_file.stat().st_size > 0
    assert (backup_bundles / str(expected_bundle) / "manifest.json").is_file()
    expected_files = _file_hashes(bundle_root)
    assert expected_files == _file_hashes(backup_bundles)

    # Keep the original files recoverable while proving the service closes on artifact loss.
    bundle_root.rename(tmp_path / "withheld-bundles")
    without_artifacts = build_demo_application()
    without_artifacts.manager.recover()
    assert without_artifacts.manager.publication_state()["admission_open"] is False

    # This schema was created by isolated_database for this test alone.
    with psycopg.connect(isolated_database, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    subprocess.run(
        [_pg_tool("pg_restore"), "--exit-on-error", "--no-owner", "--no-acl",
         f"--dbname={database_name}", str(backup_file)],
        env=environment, check=True, capture_output=True, text=True, timeout=30,
    )
    shutil.copytree(backup_bundles, bundle_root)
    assert _file_hashes(bundle_root) == expected_files

    restored = build_demo_application()
    restored.manager.recover()
    state = restored.manager.publication_state()
    assert state["active_bundle_id"] == expected_bundle
    assert state["admission_open"] is True

    async def check_http():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=restored)),
                                     base_url="http://test") as client:
            assert (await client.get("/health/ready")).status_code == 200
            session = (await client.post("/api/v1/sessions")).json()
            response = await client.post("/api/v1/recommendations", headers={
                "X-Session-Token": session["access_token"],
            }, json={"session_id": session["session_id"], "expected_history_version": 0,
                     "strategy": "popular", "k": 1})
            assert response.status_code == 200, response.text
            assert [item["item_id"] for item in response.json()["items"]] == ["restored-item"]

    asyncio.run(check_http())
