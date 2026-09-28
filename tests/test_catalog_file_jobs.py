import asyncio
import os
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import psycopg
import pytest

from evorec.api.app import create_app
from evorec.bootstrap import build_demo_application
from evorec.api.catalog_file import parse_catalog_file


def test_file_job_lifecycle_is_durable_atomic_and_idempotent(isolated_database, monkeypatch):
    monkeypatch.setenv("EVOREC_ADMIN_TOKEN", "local-admin-test-token-32-characters")
    admin = {"X-Admin-Token": "local-admin-test-token-32-characters"}
    batch = str(uuid4())
    content = b"item_id,title,category\nfirst,First,test\nsecond,Second,test\n"

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),
                                     base_url="http://test") as client:
            path = "/api/v1/admin/catalog/file-import-jobs"
            headers = {**admin, "X-Batch-Id": batch, "Content-Type": "text/csv"}
            assert (await client.post(path, headers={"X-Batch-Id": batch}, content=content)).status_code == 403
            queued = await client.post(path, headers=headers, content=content)
            assert queued.status_code == 202, queued.text
            assert queued.json()["status"] == "queued"
            assert queued.json()["attempts"] == 0
            assert (await client.get(f"{path}/{batch}")).status_code == 403
            replay = await client.post(path, headers=headers, content=content)
            assert replay.status_code == 202
            assert replay.json()["replayed"] is True
            conflict = await client.post(path, headers=headers,
                                         content=content.replace(b"First", b"Changed"))
            assert conflict.status_code == 409
            assert (await client.get("/api/v1/items/first")).status_code == 404
            assert (await client.get(f"/api/v1/admin/catalog/imports/{batch}",
                                     headers=admin)).status_code == 404
            reserved = await client.post("/api/v1/admin/catalog/imports", headers=admin,
                                         json={"batch_id": batch, "items": [
                                             {"item_id": "first", "title": "First", "category": "test"}]})
            assert reserved.status_code == 409

        # A new process consumes the persisted task, not an in-process background callback.
        result = subprocess.run([sys.executable, "-m", "scripts.catalog_worker", "--once"],
                                env=os.environ.copy(), capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),
                                     base_url="http://test") as client:
            state = await client.get(f"{path}/{batch}", headers=admin)
            assert state.status_code == 200, state.text
            assert state.json()["status"] == "imported"
            assert state.json()["item_count"] == 2
            assert state.json()["attempts"] == 1
            assert (await client.get("/api/v1/items/first")).status_code == 200
            assert (await client.get(f"/api/v1/admin/catalog/imports/{batch}",
                                     headers=admin)).status_code == 200
            replay = await client.post(path, headers=headers, content=content)
            assert replay.status_code == 200
            assert replay.json()["replayed"] is True
            assert replay.json()["status"] == "imported"
        with psycopg.connect(isolated_database) as connection:
            row = connection.execute(
                "SELECT payload IS NULL, count(*) OVER () FROM catalog_file_jobs WHERE batch_id = %s",
                (batch,),
            ).fetchone()
            assert row == (True, 1)
            assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 2

    asyncio.run(exercise())


def test_failed_file_job_keeps_row_errors_and_zero_writes(isolated_database, monkeypatch):
    monkeypatch.setenv("EVOREC_ADMIN_TOKEN", "local-admin-test-token-32-characters")
    admin = {"X-Admin-Token": "local-admin-test-token-32-characters"}
    batch = uuid4()
    service = build_demo_application().manager.file_jobs
    content = b"item_id,title,category\nfirst,First,test\nfirst,Again,test\nthird,,test\n"
    queued = service.enqueue(batch, content, "text/csv")
    assert queued["status"] == "queued"
    failed = service.process(batch, parse_catalog_file)
    assert failed["status"] == "failed"
    assert failed["error_code"] == "invalid_catalog_items"
    assert {row["field"] for row in failed["row_errors"]} == {"item_id", "title"}
    assert service.enqueue(batch, content, "text/csv")["replayed"] is True
    with psycopg.connect(isolated_database) as connection:
        assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 0
        assert connection.execute("SELECT payload IS NULL FROM catalog_file_jobs").fetchone()[0]

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),
                                     base_url="http://test") as client:
            state = await client.get(f"/api/v1/admin/catalog/file-import-jobs/{batch}",
                                     headers=admin)
            assert state.status_code == 200
            assert len(state.json()["row_errors"]) == 2
            assert (await client.get(f"/api/v1/admin/catalog/imports/{batch}",
                                     headers=admin)).status_code == 404

    asyncio.run(check())


def test_interrupted_validation_requeues_same_bytes(isolated_database):
    application = build_demo_application()
    service = application.manager.file_jobs
    batch = uuid4()
    content = b"item_id,title,category\nrecovered,Recovered,test\n"
    service.enqueue(batch, content, "text/csv")
    with psycopg.connect(isolated_database) as connection:
        connection.execute("UPDATE catalog_file_jobs SET status = 'validating', attempts = 1 "
                           "WHERE batch_id = %s", (batch,))
    service.recover_interrupted()
    assert service.get(batch)["status"] == "queued"
    assert service.run_next(parse_catalog_file) is True
    assert service.get(batch)["status"] == "imported"
    assert service.get(batch)["attempts"] == 2


def test_crash_after_import_commit_replays_without_duplicate_items(isolated_database):
    application = build_demo_application()
    service = application.manager.file_jobs
    batch = uuid4()
    content = b"item_id,title,category\ncommitted,Committed,test\n"
    service.enqueue(batch, content, "text/csv")
    # Simulate a worker dying after the atomic import but before recording job completion.
    application.manager.import_items(parse_catalog_file(content, "text/csv", batch),
                                     from_job=True)
    with psycopg.connect(isolated_database) as connection:
        connection.execute("UPDATE catalog_file_jobs SET status = 'validating', attempts = 1 "
                           "WHERE batch_id = %s", (batch,))
    service.recover_interrupted()
    assert service.run_next(parse_catalog_file) is True
    assert service.get(batch)["status"] == "imported"
    with psycopg.connect(isolated_database) as connection:
        assert connection.execute("SELECT count(*) FROM items WHERE item_id = 'committed'").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM catalog_imports WHERE batch_id = %s",
                                  (batch,)).fetchone()[0] == 1


def test_killed_validation_worker_retries_same_file_without_partial_import(isolated_database):
    service = build_demo_application().manager.file_jobs
    batch = uuid4()
    service.enqueue(batch, b"item_id,title,category\nrecovered,Recovered,test\n", "text/csv")
    stalled_worker = (
        "import time\n"
        "import scripts.catalog_worker as worker\n"
        "def pause(data, media_type, batch_id):\n"
        "    time.sleep(60)\n"
        "worker.parse_catalog_file = pause\n"
        "raise SystemExit(worker.main())\n"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", stalled_worker, "--once"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if service.get(batch)["status"] == "validating":
                break
            if worker.poll() is not None:
                stdout, stderr = worker.communicate()
                pytest.fail(f"worker exited before validating: {stdout} {stderr}")
            time.sleep(0.05)
        else:
            pytest.fail("worker did not reach the validating state")

        worker.kill()
        worker.communicate(timeout=5)
        assert service.get(batch)["status"] == "validating"
        with psycopg.connect(isolated_database) as connection:
            assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 0

        restarted = subprocess.run(
            [sys.executable, "-m", "scripts.catalog_worker", "--once"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert restarted.returncode == 0, restarted.stderr + restarted.stdout
        assert service.get(batch)["status"] == "imported"
        assert service.get(batch)["attempts"] == 2
        with psycopg.connect(isolated_database) as connection:
            assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 1
            assert connection.execute(
                "SELECT payload IS NULL FROM catalog_file_jobs WHERE batch_id = %s", (batch,),
            ).fetchone()[0]
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=5)
