"""Actual PostgreSQL transactions with real controlled synthetic R06 packages."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
import pytest

from evorec.api.app import create_app
from evorec.bootstrap import build_demo_application
from evorec.contracts import CatalogImportInput
from evorec.domain.errors import ManagementError
from evorec.infrastructure.r06_bundle import KIND
from evorec.infrastructure.r06_catalog import CATEGORY
from test_r06_bundle import _build


@pytest.fixture
def prepared_setup(isolated_database, tmp_path, monkeypatch):
    managed, target, digest = _build(tmp_path)
    monkeypatch.setenv("EVOREC_BUNDLE_ROOT", str(managed))
    monkeypatch.setenv("EVOREC_ADMIN_TOKEN", "local-admin-test-token-32-characters")
    application = build_demo_application()
    return application, UUID(target.name), digest, isolated_database


def _counts(url):
    with psycopg.connect(url, row_factory=dict_row) as c:
        return (c.execute("SELECT count(*) AS n FROM items").fetchone()["n"],
                c.execute("SELECT count(*) AS n FROM bundle_versions").fetchone()["n"],
                c.execute("SELECT count(*) AS n FROM bundle_items").fetchone()["n"])


def test_atomic_prepare_persisted_and_replayed_after_restart(prepared_setup):
    app, identity, digest, url = prepared_setup
    before = app.manager.publication_state()
    result = app.manager.prepare_r06_bundle(identity, digest)
    assert result == dict(bundle_id=identity, manifest_sha256=digest, item_count=6, replayed=False)
    assert app.manager.publication_state() == before and app.backend.runtime is None
    assert _counts(url) == (6, 1, 6)
    with app.manager._connect() as c:
        row = c.execute("SELECT runtime_kind, status FROM bundle_versions WHERE bundle_id = %s", (identity,)).fetchone()
        assert row == dict(runtime_kind=KIND, status="ready")
        items = c.execute("SELECT item_id, title, category, description, r06_model_text, r06_first_seen_ms FROM items ORDER BY item_id").fetchall()
        assert items[0] == dict(item_id="a", title="中文 alpha", category=CATEGORY,
                               description="中文 alpha", r06_model_text="中文 alpha", r06_first_seen_ms=1)
        assert items[2]["title"] == "c" and items[2]["r06_model_text"] == ""
        runtime = app.manager.r06.load_registered(c, identity)
        assert len(runtime.catalog_item_sha256) == 6
    restarted = build_demo_application()
    assert restarted.manager.prepare_r06_bundle(identity, digest)["replayed"] is True
    assert _counts(url) == (6, 1, 6)


def test_new_preparation_collects_scoped_planner_statistics_without_publishing(prepared_setup):
    app, identity, digest, _ = prepared_setup
    before = app.manager.publication_state()
    app.manager.prepare_r06_bundle(identity, digest)
    with app.manager._connect() as connection:
        rows = connection.execute(
            "SELECT relname, reltuples FROM pg_class "
            "WHERE oid IN ('items'::regclass, 'bundle_items'::regclass) ORDER BY relname"
        ).fetchall()
        assert rows == [dict(relname="bundle_items", reltuples=6.), dict(relname="items", reltuples=6.)]
        assert len(connection.execute(
            "SELECT attname FROM pg_stats WHERE schemaname=current_schema() "
            "AND tablename='bundle_items' AND attname='bundle_id'"
        ).fetchall()) == 1
    assert app.manager.publication_state() == before and app.backend.runtime is None
    # Statistics are not a content admission claim: normal replay must still
    # reject changed actual model inputs, and leave the pointer untouched.
    with app.manager._connect() as connection:
        connection.execute("UPDATE items SET r06_model_text='changed' WHERE item_id='a'")
    with pytest.raises(ManagementError) as error:
        app.manager.prepare_r06_bundle(identity, digest)
    assert error.value.code == "r06_catalog_changed"
    assert app.manager.publication_state() == before


def test_prepare_api_requires_admin_approval_and_keeps_unready(prepared_setup):
    _, identity, digest, url = prepared_setup
    async def run():
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = f"/api/v1/admin/r06/bundles/{identity}/prepare"
            body = {"expected_manifest_sha256": digest}
            admin = {"X-Admin-Token": "local-admin-test-token-32-characters"}
            assert (await client.post(path, json=body)).status_code == 403
            for invalid in ({}, {"expected_manifest_sha256": "A"*64},
                            {**body, "activate": True}, {"expected_manifest_sha256": 1}):
                assert (await client.post(path, headers=admin, json=invalid)).status_code == 422
            wrong = await client.post(path, headers=admin, json={"expected_manifest_sha256": "a"*64})
            assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "component_changed"
            assert _counts(url) == (0, 0, 0)
            prepared = await client.post(path, headers=admin, json=body)
            assert prepared.status_code == 200 and prepared.json()["item_count"] == 6
            assert (await client.get("/health/ready")).status_code == 503
            replay = await client.post(path, headers=admin, json=body)
            assert replay.status_code == 200 and replay.json()["replayed"]
            pub = await client.post(f"/api/v1/admin/bundles/{identity}/publish", headers=admin,
                                   json={"operation_id": str(uuid4()), "expected_active_bundle_id": None})
            assert pub.status_code == 503 and pub.json()["error"]["code"] == "r06_online_not_enabled"
            state = await client.get("/api/v1/admin/publication", headers=admin)
            assert state.json()["active_bundle_id"] is None and state.json()["pending_operation"] is None
    asyncio.run(run())


def test_existing_ordinary_item_conflict_rolls_back_everything(prepared_setup):
    app, identity, digest, url = prepared_setup
    app.manager.import_items(CatalogImportInput(batch_id=uuid4(), items=[
        {"item_id": "zero", "title": "Unrelated ordinary item", "category": "test"}]))
    with pytest.raises(ManagementError) as error:
        app.manager.prepare_r06_bundle(identity, digest)
    assert error.value.code == "r06_item_conflict"
    assert _counts(url) == (1, 0, 0)
    with app.manager._connect() as c:
        row = c.execute("SELECT title, r06_model_text FROM items WHERE item_id='zero'").fetchone()
        assert row == dict(title="Unrelated ordinary item", r06_model_text=None)


@pytest.mark.parametrize("field,value", [
    ("r06_model_text", "modified"), ("r06_first_seen_ms", 2), ("title", "modified"),
    ("category", "modified"), ("description", "modified"), ("image_url", "https://example.org/image"),
])
def test_actual_database_representation_drift_rejected(prepared_setup, field, value):
    from psycopg import sql
    app, identity, digest, _ = prepared_setup
    app.manager.prepare_r06_bundle(identity, digest)
    with app.manager._connect() as c:
        c.execute(sql.SQL("UPDATE items SET {} = %s WHERE item_id='a'").format(sql.Identifier(field)), (value,))
    with pytest.raises(ManagementError) as error:
        app.manager.prepare_r06_bundle(identity, digest)
    assert error.value.code == "r06_catalog_changed"
    with app.manager._connect() as c, pytest.raises(ManagementError):
        app.manager.r06.load_registered(c, identity)


@pytest.mark.parametrize("mutation", ["missing_member", "internal_gap", "hash", "path", "kind", "building"])
def test_registration_and_membership_changes_rejected(prepared_setup, mutation):
    app, identity, digest, _ = prepared_setup
    app.manager.prepare_r06_bundle(identity, digest)
    with app.manager._connect() as c:
        if mutation == "missing_member": c.execute("DELETE FROM bundle_items WHERE bundle_id=%s AND item_id='a'", (identity,))
        elif mutation == "internal_gap": c.execute("UPDATE bundle_items SET internal_item_id=99 WHERE bundle_id=%s AND item_id='zero'", (identity,))
        elif mutation == "hash": c.execute("UPDATE bundle_versions SET manifest_sha256=%s WHERE bundle_id=%s", ("a"*64, identity))
        elif mutation == "path": c.execute("UPDATE bundle_versions SET artifact_path='outside' WHERE bundle_id=%s", (identity,))
        elif mutation == "kind": c.execute("UPDATE bundle_versions SET runtime_kind='cpu-demo-v1' WHERE bundle_id=%s", (identity,))
        elif mutation == "building": c.execute("UPDATE bundle_versions SET status='building', manifest_sha256=NULL WHERE bundle_id=%s", (identity,))
    with pytest.raises(ManagementError): app.manager.prepare_r06_bundle(identity, digest)
    with app.manager._connect() as c, pytest.raises(ManagementError):
        app.manager.r06.load_registered(c, identity)


def test_inactive_item_is_not_reactivated_by_prepare_replay(prepared_setup):
    app, identity, digest, _ = prepared_setup
    app.manager.prepare_r06_bundle(identity, digest)
    app.manager.deactivate_item("a")
    state = app.manager.publication_state()
    assert app.manager.prepare_r06_bundle(identity, digest)["replayed"]
    assert app.manager.publication_state() == state
    with app.manager._connect() as c:
        assert c.execute("SELECT is_active FROM items WHERE item_id='a'").fetchone()["is_active"] is False


def test_concurrent_same_preparation_has_one_commit_and_one_replay(prepared_setup):
    app, identity, digest, url = prepared_setup
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: app.manager.prepare_r06_bundle(identity, digest), range(2)))
    assert sorted(row["replayed"] for row in results) == [False, True]
    assert _counts(url) == (6, 1, 6)


def test_late_verification_failure_rolls_back_rows_and_registration(prepared_setup, monkeypatch):
    from evorec.infrastructure import r06_catalog as module
    app, identity, digest, url = prepared_setup
    def fail(*a): raise ManagementError("injected_failure", "synthetic precommit failure")
    monkeypatch.setattr(module, "verify_database_sources", fail)
    with pytest.raises(ManagementError): app.manager.prepare_r06_bundle(identity, digest)
    assert _counts(url) == (0, 0, 0)
    assert app.manager.publication_state()["active_bundle_id"] is None


def test_preparation_does_not_replace_existing_active_demo(prepared_setup):
    app, identity, digest, _ = prepared_setup
    batch = uuid4()
    app.manager.import_items(CatalogImportInput(batch_id=batch, items=[
        {"item_id": "demo-other", "title": "Kept demo", "category": "test"}]))
    build = app.manager.builds.process(batch, uuid4())
    app.manager.builds.publish(build["build_id"], uuid4())
    before = app.manager.publication_state()
    runtime = app.backend.runtime
    app.manager.prepare_r06_bundle(identity, digest)
    assert app.manager.publication_state() == before and app.backend.runtime is runtime
    with pytest.raises(ManagementError) as error:
        app.manager.publish(uuid4(), identity, before["active_bundle_id"])
    assert error.value.code == "r06_online_not_enabled"
    assert app.manager.publication_state() == before


def test_restart_closes_manually_forced_unservable_r06_pointer(prepared_setup):
    app, identity, digest, _ = prepared_setup
    app.manager.prepare_r06_bundle(identity, digest)
    with app.manager._connect() as c:
        c.execute("UPDATE bundle_versions SET status='active' WHERE bundle_id=%s", (identity,))
        c.execute("UPDATE catalog_control SET active_bundle_id=%s, admission_open=true", (identity,))
    restarted = build_demo_application()
    restarted.manager.recover()
    assert restarted.manager.publication_state()["admission_open"] is False
    assert restarted.backend.runtime is None


def test_bundle_byte_drift_never_prepares_database(prepared_setup):
    app, identity, digest, url = prepared_setup
    (app.manager.managed_root / str(identity) / "features/vectors.f32").write_bytes(b"tamper")
    with pytest.raises(ManagementError) as error: app.manager.prepare_r06_bundle(identity, digest)
    assert error.value.status_code == 422
    assert _counts(url) == (0, 0, 0)


@pytest.fixture
def verifier_setup(prepared_setup, tmp_path, monkeypatch):
    from scripts import verify_r06_catalog_preparation as verifier
    app, identity, digest, url = prepared_setup
    # This fixture tests reporting/cleanup, not clean-source experimental provenance.
    project = tmp_path / "fake-project"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts/source.py").write_bytes(b"synthetic source fixture")
    monkeypatch.setattr(verifier, "__file__", str(project / "scripts/verify.py"))
    monkeypatch.setattr(verifier, "SOURCE_FILES", ("scripts/source.py",))
    monkeypatch.setattr(verifier, "_source", lambda _: "a"*40)
    return verifier, project / "artifacts/run", app.manager.managed_root, identity, digest, url


def _schemas(url):
    with psycopg.connect(url) as c:
        return {row[0] for row in c.execute("SELECT schema_name FROM information_schema.schemata").fetchall()}


def test_verifier_uses_owned_schema_and_checks_real_stored_sources(verifier_setup):
    import json
    verifier, output, managed, identity, digest, url = verifier_setup
    before = _schemas(url)
    result = verifier.verify(output, url, managed, identity, digest)
    assert result["status"] == "passed" and result["item_count"] == 6
    assert result["owned_temporary_schema_removed"] and result["database_sources_exact"]
    assert result["real_database_drift_rejected"] and result["unservable_publication_rejected"]
    assert result["activated"] is False and result["business_schema_untouched"]
    assert json.loads((output / "verification.json").read_text()) == result
    assert _schemas(url) == before and _counts(url) == (0, 0, 0)


@pytest.mark.parametrize("failure", ["prepare", "cleanup", "source"])
def test_verifier_never_reports_passed_after_failure(verifier_setup, monkeypatch, failure):
    from psycopg import sql
    verifier, output, managed, identity, digest, url = verifier_setup
    before = _schemas(url)
    connect = psycopg.connect
    if failure == "prepare":
        def fail(*args): raise ValueError("synthetic preparation failure")
        monkeypatch.setattr(verifier.CatalogManager, "prepare_r06_bundle", fail)
    elif failure == "source":
        calls = iter(["a"*40, "b"*40])
        monkeypatch.setattr(verifier, "_source", lambda _: next(calls))
    else:
        class RefuseDrop:
            def __init__(self, connection): self.connection = connection
            def __getattr__(self, name): return getattr(self.connection, name)
            def __enter__(self): self.connection.__enter__(); return self
            def __exit__(self, *args): return self.connection.__exit__(*args)
            def execute(self, query, *args):
                if isinstance(query, sql.Composed) and query.as_string(self.connection).startswith("DROP SCHEMA"):
                    raise psycopg.OperationalError("synthetic cleanup failure")
                return self.connection.execute(query, *args)
        monkeypatch.setattr(verifier.psycopg, "connect", lambda *a, **k: RefuseDrop(connect(*a, **k)))
    try:
        with pytest.raises((ValueError, psycopg.OperationalError)):
            verifier.verify(output, url, managed, identity, digest)
        assert not (output / "verification.json").exists()
        assert (output / "source/scripts/source.py").exists()
    finally:
        # Cleanup the one deliberately stranded, uniquely owned fixture schema.
        monkeypatch.setattr(verifier.psycopg, "connect", connect)
        remaining = _schemas(url) - before
        assert len(remaining) == (1 if failure == "cleanup" else 0)
        for schema in remaining:
            assert schema.startswith("test_evorec_") and len(schema) == 44
            with connect(url) as c:
                c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    assert _schemas(url) == before and _counts(url) == (0, 0, 0)


def test_verifier_preserves_existing_destination_before_database_access(verifier_setup, monkeypatch):
    verifier, output, managed, identity, digest, url = verifier_setup
    output.mkdir(parents=True)
    report = output / "verification.json"
    report.write_bytes(b"older evidence")
    def unexpected(*a, **k): raise AssertionError("database accessed")
    with monkeypatch.context() as scope:
        scope.setattr(verifier.psycopg, "connect", unexpected)
        with pytest.raises(FileExistsError): verifier.verify(output, url, managed, identity, digest)
    assert report.read_bytes() == b"older evidence"
