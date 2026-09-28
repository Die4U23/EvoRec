import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import subprocess
import sys
from threading import Event
import time
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest

from evorec.api.app import create_app
from evorec.bootstrap import build_demo_application
from evorec.contracts import CatalogImportInput
from evorec.domain.errors import ManagementError
from evorec.infrastructure import catalog_build
from evorec.infrastructure.bundle import validate_bundle
from evorec.infrastructure.catalog_builder import build_catalog_bundle, content_vector
from evorec.infrastructure.model_runtime import load_runtime_bundle

ADMIN = {"X-Admin-Token": "local-admin-test-token-32-characters"}


def imported(manager, *ids):
    batch = uuid4()
    manager.import_items(CatalogImportInput(batch_id=batch, items=[
        {"item_id": item_id, "title": "Cooperative puzzle game" if item_id != "other" else "Racing car",
         "category": "puzzle" if item_id != "other" else "racing"}
        for item_id in ids]))
    return batch


@pytest.fixture
def managed(isolated_database, monkeypatch, tmp_path):
    monkeypatch.setenv("EVOREC_ADMIN_TOKEN", ADMIN["X-Admin-Token"])
    monkeypatch.setenv("EVOREC_BUNDLE_ROOT", str(tmp_path / "bundles"))
    return build_demo_application()


def test_content_bundle_has_real_metadata_similarity_and_auditable_input(tmp_path):
    items = [{"item_id": item_id, "title": title, "category": category}
             for item_id, title, category in [
                 ("history", "合作解谜游戏", "解谜"), ("new", "合作解谜游戏", "解谜"),
                 ("other", "赛车竞速", "赛车")]]
    assert content_vector(items[0]) == content_vector(items[1])
    bundle_id = uuid4()
    progress = []
    build_catalog_bundle(tmp_path, bundle_id, uuid4(), items, progress.append)
    runtime = load_runtime_bundle(validate_bundle(tmp_path, tmp_path / str(bundle_id)))
    scores = runtime.score(["history"], ["new", "other"])
    assert scores[0] == pytest.approx(1.0)
    assert scores[1] < scores[0]
    assert len(set(runtime.semantic_codes)) == 3
    assert progress == [3]
    manifest = json.loads((tmp_path / str(bundle_id) / "manifest.json").read_bytes())
    snapshot = (tmp_path / str(bundle_id) / "catalog.json").read_bytes()
    assert hashlib.sha256(snapshot).hexdigest() == manifest["source"]["dataset_sha256"]
    assert (tmp_path / str(bundle_id) / "builder-source.txt").is_file()
    assert (tmp_path / str(bundle_id) / "runtime-source.txt").is_file()
    assert (tmp_path / str(bundle_id) / "manager-source.txt").is_file()


def test_import_build_preview_publish_and_restart_reaches_cold_item(managed):
    manager = managed.manager
    initial = imported(manager, "history", "other")
    first = manager.builds.process(initial, uuid4())
    manager.builds.publish(first["build_id"], uuid4())

    async def exercise():
        app = create_app(demo_application=managed)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            session = (await client.post("/api/v1/sessions")).json()
            auth = {"X-Session-Token": session["access_token"]}
            request = {"session_id": session["session_id"], "expected_history_version": 0,
                       "strategy": "popular", "k": 2}
            old = (await client.post("/api/v1/recommendations", headers=auth, json=request)).json()
            feedback = await client.post("/api/v1/feedback", headers=auth, json={
                "event_id": str(uuid4()), "session_id": session["session_id"],
                "request_id": old["request_id"], "item_id": "history", "kind": "detail_view",
                "observed_at": "2026-09-26T00:00:00Z"})
            assert feedback.status_code == 200
            request.update(expected_history_version=feedback.json()["history_version"], strategy="dense")
            batch = imported(manager, "cold")
            build_id = uuid4()
            route = f"/api/v1/admin/catalog/imports/{batch}/builds"
            assert (await client.post(route, json={"build_id": str(build_id)})).status_code == 403
            response = await client.post(route, headers=ADMIN, json={"build_id": str(build_id)})
            assert response.status_code == 200, response.text
            build = response.json()
            assert build["status"] == "ready" and build["total_count"] == 3
            assert build["base_bundle_id"] == str(first["bundle_id"])
            assert (await client.post(route, headers=ADMIN, json={"build_id": str(build_id)})).json() == build
            preview = await client.get(f"/api/v1/admin/catalog/builds/{build_id}/items", headers=ADMIN)
            assert preview.status_code == 200
            cold = next(item for item in preview.json()["items"] if item["item_id"] == "cold")
            assert cold["ready_for_publication"] and not cold["currently_recommendable"]
            before = (await client.post("/api/v1/recommendations", headers=auth, json=request)).json()
            assert "cold" not in {item["item_id"] for item in before["items"]}
            operation = str(uuid4())
            published = await client.post(f"/api/v1/admin/catalog/builds/{build_id}/publish", headers=ADMIN,
                                          json={"operation_id": operation})
            assert published.status_code == 200, published.text
            repeated = await client.post(f"/api/v1/admin/catalog/builds/{build_id}/publish", headers=ADMIN,
                                         json={"operation_id": operation})
            assert repeated.json()["replayed"] is True
            after = (await client.post("/api/v1/recommendations", headers=auth, json=request)).json()
            assert after["actual_strategy"] == "dense"
            assert after["items"][0]["item_id"] == "cold"
            assert after["bundle_id"] == build["bundle_id"]
            assert before["bundle_id"] == str(first["bundle_id"])

        restarted = create_app()
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://test") as client:
                status = (await client.get(f"/api/v1/admin/catalog/builds/{build_id}", headers=ADMIN)).json()
                assert status["publication_status"] == "active"
                restored = await client.post("/api/v1/recommendations", headers=auth, json=request)
                assert restored.status_code == 200
                assert restored.json()["items"][0]["item_id"] == "cold"

    asyncio.run(exercise())


def test_failed_build_preserves_old_version_and_retries_frozen_snapshot(managed, monkeypatch):
    manager = managed.manager
    first = manager.builds.process(imported(manager, "history"), uuid4())
    manager.builds.publish(first["build_id"], uuid4())
    batch = imported(manager, "cold")
    build_id = uuid4()
    original = catalog_build.build_catalog_bundle

    def fail(root, bundle_id, task, items, progress):
        progress(1)
        raise OSError("injected disk failure")

    monkeypatch.setattr(catalog_build, "build_catalog_bundle", fail)
    with pytest.raises(ManagementError, match="build failed"):
        manager.builds.process(batch, build_id)
    status = manager.builds.get(build_id)
    assert (status["status"], status["processed_count"], status["failed_count"]) == ("failed", 1, 1)
    assert manager.publication_state()["active_bundle_id"] == first["bundle_id"]
    assert manager.publication_state()["admission_open"]
    imported(manager, "unrelated")
    monkeypatch.setattr(catalog_build, "build_catalog_bundle", original)
    retried = manager.builds.process(batch, build_id)
    assert retried["attempts"] == 2 and retried["status"] == "ready"
    assert {item["item_id"] for item in manager.builds.preview(build_id, 0, 50)["items"]} == {"history", "cold"}


def test_stale_build_rejected_and_deactivation_survives_version_change(managed):
    manager = managed.manager
    first = manager.builds.process(imported(manager, "history"), uuid4())
    manager.builds.publish(first["build_id"], uuid4())
    batch_a = imported(manager, "a")
    build_a = manager.builds.process(batch_a, uuid4())
    build_b = manager.builds.process(imported(manager, "b"), uuid4())
    manager.builds.publish(build_b["build_id"], uuid4())
    with pytest.raises(ManagementError) as conflict:
        manager.builds.publish(build_a["build_id"], uuid4())
    assert conflict.value.code == "publication_version_conflict"
    with pytest.raises(ManagementError) as bypass:
        manager.publish(uuid4(), build_a["bundle_id"], build_b["bundle_id"])
    assert bypass.value.code == "build_base_conflict"
    rebuilt = manager.builds.process(batch_a, uuid4())
    assert rebuilt["total_count"] == 3
    manager.deactivate_item("history")
    manager.builds.publish(rebuilt["build_id"], uuid4())
    manager.publish(uuid4(), build_b["bundle_id"], rebuilt["bundle_id"])
    history = next(item for item in manager.builds.preview(build_b["build_id"], 0, 50)["items"]
                   if item["item_id"] == "history")
    assert not history["is_active"] and not history["currently_recommendable"]


def test_guarded_rollback_restores_old_version_without_reactivating_items(managed):
    manager = managed.manager
    first = manager.builds.process(imported(manager, "history"), uuid4())
    manager.builds.publish(first["build_id"], uuid4())
    second = manager.builds.process(imported(manager, "cold"), uuid4())
    manager.builds.publish(second["build_id"], uuid4())
    never_published = manager.builds.process(imported(manager, "unpublished"), uuid4())
    manager.deactivate_item("history")

    async def exercise():
        app = create_app(demo_application=managed)
        target = f"/api/v1/admin/bundles/{first['bundle_id']}/rollback"
        payload = {"operation_id": str(uuid4()),
                   "expected_active_bundle_id": str(second["bundle_id"])}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.post(target, json=payload)).status_code == 403
            unpublished = await client.post(
                f"/api/v1/admin/bundles/{never_published['bundle_id']}/rollback",
                headers=ADMIN, json=payload)
            assert unpublished.status_code == 409
            assert unpublished.json()["error"]["code"] == "rollback_target_unpublished"
            invalid = await client.post(target, headers=ADMIN, json={
                "operation_id": str(uuid4()), "expected_active_bundle_id": None})
            assert invalid.status_code == 409
            assert invalid.json()["error"]["code"] == "rollback_target_invalid"
            stale = await client.post(target, headers=ADMIN, json={
                "operation_id": str(uuid4()),
                "expected_active_bundle_id": str(never_published["bundle_id"])})
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "publication_version_conflict"
            rolled_back = await client.post(target, headers=ADMIN, json=payload)
            assert rolled_back.status_code == 200, rolled_back.text
            assert rolled_back.json()["active_bundle_id"] == str(first["bundle_id"])
            assert (await client.post(target, headers=ADMIN, json=payload)).json()["replayed"]
            assert manager.publication_state()["active_bundle_id"] == first["bundle_id"]
            assert manager.get_item("history")["is_active"] is False
            preview = manager.builds.preview(first["build_id"], 0, 10)
            assert preview["items"][0]["currently_recommendable"] is False

    asyncio.run(exercise())


def test_running_build_exposes_progress_and_rejects_parallel_worker(managed, monkeypatch):
    manager = managed.manager
    batch = imported(manager, "cold")
    build_id = uuid4()
    entered, release = Event(), Event()
    original = catalog_build.build_catalog_bundle

    def pause(root, bundle_id, task, items, progress):
        progress(1)
        entered.set()
        assert release.wait(10), "test did not release the build"
        return original(root, bundle_id, task, items, progress)

    monkeypatch.setattr(catalog_build, "build_catalog_bundle", pause)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(manager.builds.process, batch, build_id)
        try:
            assert entered.wait(10)
            manager.builds.recover_interrupted()
            assert manager.builds.get(build_id)["status"] == "processing"
            assert manager.builds.get(build_id)["processed_count"] == 1
            with pytest.raises(ManagementError) as busy:
                manager.builds.process(batch, uuid4())
            assert busy.value.code == "build_in_progress"
        finally:
            release.set()
        assert pending.result(timeout=10)["status"] == "ready"


def test_interrupted_record_is_recoverable_and_legacy_import_is_explicit(managed):
    manager = managed.manager
    batch = imported(manager, "cold")
    ready = manager.builds.process(batch, uuid4())
    with psycopg.connect(manager.database_url) as connection:
        connection.execute("UPDATE catalog_builds SET status = 'processing' WHERE build_id = %s",
                           (ready["build_id"],))
    manager.builds.recover_interrupted()
    assert manager.builds.get(ready["build_id"])["error_code"] == "build_interrupted"
    retry = manager.builds.process(batch, ready["build_id"])
    assert retry["attempts"] == 2 and retry["bundle_id"] != ready["bundle_id"]
    with pytest.raises(ManagementError):
        manager.publish(uuid4(), ready["bundle_id"], None)
    legacy = uuid4()
    with psycopg.connect(manager.database_url) as connection:
        connection.execute("INSERT INTO catalog_imports (batch_id, payload_sha256, item_count) VALUES (%s,%s,1)",
                           (legacy, "0" * 64))
    with pytest.raises(ManagementError) as missing:
        manager.builds.process(legacy, uuid4())
    assert missing.value.code == "legacy_import_without_snapshot"
    assert manager.get_import(legacy)["snapshot_available"] is False


def test_deleted_import_item_cannot_produce_an_incomplete_bundle(managed):
    manager = managed.manager
    batch = imported(manager, "cold")
    with psycopg.connect(manager.database_url) as connection:
        connection.execute("DELETE FROM items WHERE item_id = 'cold'")
    with pytest.raises(ManagementError) as missing:
        manager.builds.process(batch, uuid4())
    assert missing.value.code == "import_items_missing"
    with psycopg.connect(manager.database_url) as connection:
        assert connection.execute("SELECT count(*) FROM catalog_builds").fetchone()[0] == 0


def test_durable_queue_acknowledges_before_build_and_publishes_only_after_confirmation(managed):
    manager = managed.manager
    batch = imported(manager, "cold")
    build_id = uuid4()

    async def exercise():
        app = create_app(demo_application=managed)
        route = f"/api/v1/admin/catalog/imports/{batch}/build-jobs"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            payload = {"build_id": str(build_id)}
            assert (await client.post(route, json=payload)).status_code == 403
            queued = await client.post(route, headers=ADMIN, json=payload)
            assert queued.status_code == 202, queued.text
            assert queued.json()["status"] == "queued"
            assert queued.json()["processed_count"] == 0
            assert (await client.post(route, headers=ADMIN, json=payload)).json() == queued.json()
            assert manager.publication_state()["active_bundle_id"] is None
            assert manager.builds.run_next()
            assert not manager.builds.run_next()
            ready = (await client.get(f"/api/v1/admin/catalog/builds/{build_id}", headers=ADMIN)).json()
            assert ready["status"] == "ready"
            assert ready["attempts"] == 1
            assert ready["publication_status"] == "ready"
            import_state = await client.get(f"/api/v1/admin/catalog/imports/{batch}", headers=ADMIN)
            assert import_state.status_code == 200
            assert import_state.json()["latest_build"]["build_id"] == str(build_id)
            assert import_state.json()["latest_build"]["publication_status"] == "ready"
            replayed = await client.post(route, headers=ADMIN, json=payload)
            assert replayed.status_code == 200
            assert replayed.json() == ready
            assert manager.publication_state()["active_bundle_id"] is None
            published = await client.post(f"/api/v1/admin/catalog/builds/{build_id}/publish",
                                          headers=ADMIN, json={"operation_id": str(uuid4())})
            assert published.status_code == 200, published.text
            assert published.json()["active_bundle_id"] == ready["bundle_id"]
            active_import = await client.get(f"/api/v1/admin/catalog/imports/{batch}", headers=ADMIN)
            assert active_import.json()["latest_build"]["publication_status"] == "active"

    asyncio.run(exercise())


def test_interrupted_queued_job_retries_frozen_snapshot_automatically(managed):
    manager = managed.manager
    batch = imported(manager, "cold")
    build_id = uuid4()
    queued = manager.builds.enqueue(batch, build_id)
    assert manager.get_import(batch)["latest_build"]["build_id"] == build_id
    imported(manager, "unrelated")
    with psycopg.connect(manager.database_url) as connection:
        connection.execute(
            "UPDATE catalog_builds SET status = 'processing', processed_count = 1 "
            "WHERE build_id = %s", (build_id,),
        )
    manager.builds.recover_interrupted()
    interrupted = manager.builds.get(build_id)
    assert interrupted["status"] == "queued"
    assert interrupted["error_code"] == "build_interrupted"
    assert manager.builds.run_next()
    ready = manager.builds.get(build_id)
    assert ready["status"] == "ready" and ready["attempts"] == 2
    assert ready["bundle_id"] != queued["bundle_id"]
    assert {item["item_id"] for item in manager.builds.preview(build_id, 0, 10)["items"]} == {"cold"}
    assert manager.publication_state()["active_bundle_id"] is None


def test_failed_queued_job_can_be_resubmitted_with_same_id(managed, monkeypatch):
    manager = managed.manager
    batch = imported(manager, "cold")
    build_id = uuid4()
    manager.builds.enqueue(batch, build_id)
    original = catalog_build.build_catalog_bundle

    def fail(*_):
        raise OSError("injected build failure")

    monkeypatch.setattr(catalog_build, "build_catalog_bundle", fail)
    with pytest.raises(ManagementError, match="build failed"):
        manager.builds.run_next()
    assert manager.builds.get(build_id)["status"] == "failed"
    imported(manager, "unrelated")
    monkeypatch.setattr(catalog_build, "build_catalog_bundle", original)
    assert manager.builds.enqueue(batch, build_id)["status"] == "queued"
    assert manager.builds.run_next()
    assert manager.builds.get(build_id)["attempts"] == 2
    assert {item["item_id"] for item in manager.builds.preview(build_id, 0, 10)["items"]} == {"cold"}


def test_separate_worker_process_consumes_queued_build(managed):
    manager = managed.manager
    batch = imported(manager, "cold")
    build_id = uuid4()
    manager.builds.enqueue(batch, build_id)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.catalog_worker", "--once"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert manager.builds.get(build_id)["status"] == "ready"
    assert manager.publication_state()["active_bundle_id"] is None


def test_killed_worker_restarts_frozen_build_without_publishing(managed):
    manager = managed.manager
    original_batch = imported(manager, "history")
    original = manager.builds.process(original_batch, uuid4())
    manager.builds.publish(original["build_id"], uuid4())
    batch = imported(manager, "cold")
    build_id = uuid4()
    queued = manager.builds.enqueue(batch, build_id)

    stalled_worker = (
        "import time\n"
        "import evorec.infrastructure.catalog_build as catalog_build\n"
        "def pause(root, bundle_id, task, items, progress):\n"
        "    progress(1)\n"
        "    time.sleep(60)\n"
        "catalog_build.build_catalog_bundle = pause\n"
        "from scripts.catalog_worker import main\n"
        "raise SystemExit(main())\n"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", stalled_worker, "--once"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = manager.builds.get(build_id)
            if state["status"] == "processing" and state["processed_count"] == 1:
                break
            if worker.poll() is not None:
                stdout, stderr = worker.communicate()
                pytest.fail(f"worker exited before reaching build: {stdout} {stderr}")
            time.sleep(0.05)
        else:
            pytest.fail("worker did not reach the in-progress state")

        worker.kill()
        worker.communicate(timeout=5)
        assert manager.builds.get(build_id)["status"] == "processing"
        assert manager.publication_state()["active_bundle_id"] == original["bundle_id"]

        restarted = subprocess.run(
            [sys.executable, "-m", "scripts.catalog_worker", "--once"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert restarted.returncode == 0, restarted.stderr + restarted.stdout
        ready = manager.builds.get(build_id)
        assert ready["status"] == "ready"
        assert ready["attempts"] == 2
        assert ready["bundle_id"] != queued["bundle_id"]
        assert manager.publication_state()["active_bundle_id"] == original["bundle_id"]
        preview = manager.builds.preview(build_id, 0, 10)["items"]
        assert {item["item_id"] for item in preview} == {"history", "cold"}
        assert not next(item for item in preview if item["item_id"] == "cold")["currently_recommendable"]
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=5)
