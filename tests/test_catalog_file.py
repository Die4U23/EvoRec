import asyncio
import json
from uuid import uuid4

import httpx
import psycopg
import pytest

from evorec.api.app import create_app
from evorec.api.catalog_file import CatalogFileError, MAX_FILE_BYTES, parse_catalog_file


def test_catalog_file_reports_all_invalid_rows():
    content = b"item_id,title,category\nvalid,Valid,test\nbad id,,test\nvalid,Again,test\n"
    with pytest.raises(CatalogFileError) as raised:
        parse_catalog_file(content, "text/csv", uuid4())
    assert [(error.row, error.field) for error in raised.value.rows] == [
        (3, "item_id"), (3, "title"), (4, "item_id"),
    ]


@pytest.mark.parametrize("data,media_type,code", [
    (b"[]", "application/json", "invalid_item_count"),
    (b"{}", "application/json", "invalid_json"),
    (b"[", "application/json", "invalid_json"),
    (b"item_id,title\na,Alpha\n", "text/csv", "invalid_header"),
    (b"item_id,title,category\na,Alpha,test,extra\n", "text/csv", "invalid_catalog_items"),
    (b"\xff", "text/csv", "invalid_encoding"),
    (b"x", "text/plain", "unsupported_file_type"),
])
def test_catalog_file_rejects_bad_input(data, media_type, code):
    with pytest.raises(CatalogFileError) as raised:
        parse_catalog_file(data, media_type, uuid4())
    assert raised.value.code == code


def test_catalog_file_limits():
    with pytest.raises(CatalogFileError, match="validation failed") as raised:
        parse_catalog_file(b" " * (MAX_FILE_BYTES + 1), "application/json", uuid4())
    assert raised.value.code == "file_too_large"
    data = json.dumps([{"item_id": f"item-{index}", "title": "Title", "category": "test"}
                       for index in range(1001)]).encode()
    with pytest.raises(CatalogFileError) as raised:
        parse_catalog_file(data, "application/json", uuid4())
    assert raised.value.code == "invalid_item_count"


def test_catalog_file_import_is_atomic_and_idempotent(isolated_database, monkeypatch):
    monkeypatch.setenv("EVOREC_ADMIN_TOKEN", "local-admin-test-token-32-characters")
    admin = {"X-Admin-Token": "local-admin-test-token-32-characters"}
    path = "/api/v1/admin/catalog/file-imports"

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),
                                     base_url="http://test") as client:
            batch_id = str(uuid4())
            content = b"item_id,title,category\nfirst,First,test\nsecond,Second,test\n"
            headers = {**admin, "X-Batch-Id": batch_id, "Content-Type": "text/csv"}
            unauthorized = await client.post(path, headers={"X-Batch-Id": batch_id,
                                                             "Content-Type": "text/csv"}, content=content)
            assert unauthorized.status_code == 403
            imported = await client.post(path, headers=headers, content=content)
            assert imported.status_code == 200, imported.text
            assert imported.json()["item_count"] == 2
            status_path = f"/api/v1/admin/catalog/imports/{batch_id}"
            assert (await client.get(status_path)).status_code == 403
            status = await client.get(status_path, headers=admin)
            assert status.status_code == 200, status.text
            assert status.json()["item_count"] == 2
            assert status.json()["snapshot_available"] is True
            assert status.json()["latest_build"] is None
            assert status.json()["imported_at"]
            missing = await client.get(f"/api/v1/admin/catalog/imports/{uuid4()}", headers=admin)
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "import_not_found"
            replay = await client.post(path, headers=headers, content=content)
            assert replay.status_code == 200
            assert replay.json()["replayed"] is True
            changed = await client.post(path, headers=headers,
                                        content=content.replace(b"First", b"Changed"))
            assert changed.status_code == 409
            assert changed.json()["error"]["code"] == "import_conflict"

            duplicate = await client.post(
                path, headers={**headers, "X-Batch-Id": str(uuid4())},
                content=b"item_id,title,category\nfirst,Overwrite,test\nthird,Third,test\n",
            )
            assert duplicate.status_code == 409
            assert duplicate.json()["rows"] == [
                {"row": 2, "field": "item_id", "reason": "item_id already exists"}
            ]
            assert (await client.get("/api/v1/items/first")).json()["title"] == "First"
            assert (await client.get("/api/v1/items/third")).status_code == 404

            invalid = await client.post(
                path, headers={**headers, "X-Batch-Id": str(uuid4())},
                content=b"item_id,title,category\nfourth,Fourth,test\ninvalid id,,test\n",
            )
            assert invalid.status_code == 422
            assert len(invalid.json()["rows"]) == 2
            assert (await client.get("/api/v1/items/fourth")).status_code == 404

            json_import = await client.post(
                path, headers={**admin, "X-Batch-Id": str(uuid4()),
                               "Content-Type": "application/json"},
                content=json.dumps([{"item_id": "fifth", "title": "Fifth",
                                     "category": "test"}]).encode(),
            )
            assert json_import.status_code == 200
            json_duplicate = await client.post(
                "/api/v1/admin/catalog/imports", headers=admin,
                json={"batch_id": str(uuid4()), "items": [
                    {"item_id": "fifth", "title": "Changed", "category": "test"}]},
            )
            assert json_duplicate.status_code == 409
            assert json_duplicate.json()["rows"][0]["row"] == 1

            concurrent = await asyncio.gather(*[
                client.post(
                    path,
                    headers={**admin, "X-Batch-Id": str(uuid4()),
                             "Content-Type": "application/json"},
                    content=json.dumps([{"item_id": "raced", "title": "Raced",
                                         "category": "test"}]).encode(),
                ) for _ in range(2)
            ])
            assert sorted(response.status_code for response in concurrent) == [200, 409]
            assert next(response for response in concurrent if response.status_code == 409).json()[
                "rows"] == [{"row": 1, "field": "item_id", "reason": "item_id already exists"}]

            oversized = await client.post(
                path, headers={**headers, "X-Batch-Id": str(uuid4())},
                content=b"x" * (MAX_FILE_BYTES + 1),
            )
            assert oversized.status_code == 413
            assert oversized.json()["error"]["code"] == "file_too_large"

        with psycopg.connect(isolated_database) as connection:
            assert connection.execute("SELECT count(*) FROM catalog_imports").fetchone()[0] == 3
            assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 4

    asyncio.run(exercise())
