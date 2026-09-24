import asyncio
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest

from evorec.api.app import create_app
from evorec.bootstrap import build_demo_application
from tests.test_model_runtime import _bundle


def test_managed_bundle_publication_recovery_and_catalog_api(isolated_database, monkeypatch, tmp_path):
    monkeypatch.setenv("EVOREC_ADMIN_TOKEN", "local-admin-test-token-32-characters")
    managed, bundle, _ = _bundle(tmp_path)
    monkeypatch.setenv("EVOREC_BUNDLE_ROOT", str(managed))
    bundle_id = bundle.name

    async def exercise():
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            items = [
                {"item_id": "item-a", "title": "Alpha", "category": "test"},
                {"item_id": "item-b", "title": "Beta", "category": "test"},
            ]
            batch_id = str(uuid4())
            import_path = "/api/v1/admin/catalog/imports"
            unauthorized = await client.post(import_path, json={"batch_id": batch_id, "items": items})
            assert unauthorized.status_code == 403
            admin = {"X-Admin-Token": "local-admin-test-token-32-characters"}
            imported = await client.post(import_path, headers=admin,
                                         json={"batch_id": batch_id, "items": items})
            assert imported.status_code == 200
            assert imported.json()["item_count"] == 2
            listed = await client.get("/api/v1/items")
            assert listed.status_code == 200
            assert [item["item_id"] for item in listed.json()] == ["item-a", "item-b"]
            detail = await client.get("/api/v1/items/item-a")
            assert detail.status_code == 200
            assert detail.json()["title"] == "Alpha"
            denied = await client.post(import_path, headers={"X-Admin-Token": "wrong"},
                                       json={"batch_id": str(uuid4()), "items": items})
            assert denied.status_code == 403
            replay = await client.post(import_path, headers=admin,
                                       json={"batch_id": batch_id, "items": items})
            assert replay.json()["replayed"] is True
            conflict = await client.post(import_path, headers=admin,
                                         json={"batch_id": batch_id,
                                               "items": [{**items[0], "title": "Changed"}]})
            assert conflict.status_code == 409
            register = await client.post(f"/api/v1/admin/bundles/{bundle_id}/register",
                                         headers=admin)
            assert register.status_code == 200, register.text
            publish_id = str(uuid4())
            pub_path = f"/api/v1/admin/bundles/{bundle_id}/publish"
            payload = {"operation_id": publish_id, "expected_active_bundle_id": None}
            published = await client.post(pub_path, headers=admin, json=payload)
            assert published.status_code == 200, published.text
            assert published.json()["status"] == "completed"
            assert (await client.get("/health/ready")).status_code == 200
            created = (await client.post("/api/v1/sessions")).json()
            session_headers = {"X-Session-Token": created["access_token"],
                               "Idempotency-Key": str(uuid4())}
            request = {"session_id": created["session_id"],
                       "expected_history_version": 0, "strategy": "dense", "k": 2}
            result = await client.post("/api/v1/recommendations", headers=session_headers,
                                       json=request)
            assert result.status_code == 200, result.text
            assert result.json()["actual_strategy"] == "dense"
            assert [item["item_id"] for item in result.json()["items"]] == ["item-b", "item-a"]
            assert [item["score"] for item in result.json()["items"]] == pytest.approx([0.1, 0.0])
            assert {item["source"] for item in result.json()["items"]} == {
                "controlled-cpu-dot-product"
            }
            race_headers = {**session_headers, "Idempotency-Key": str(uuid4())}
            concurrent = await asyncio.gather(*(
                client.post("/api/v1/recommendations", headers=race_headers, json=request)
                for _ in range(2)
            ))
            assert all(response.status_code in {200, 409} for response in concurrent)
            assert any(response.status_code == 200 for response in concurrent)
            if any(response.status_code == 409 for response in concurrent):
                assert {response.json()["error"]["code"] for response in concurrent
                        if response.status_code == 409} == {"recommendation_in_progress"}
            final_race = await client.post("/api/v1/recommendations", headers=race_headers,
                                           json=request)
            assert final_race.status_code == 200
            assert all(response.json() == final_race.json() for response in concurrent
                       if response.status_code == 200)
            with psycopg.connect(isolated_database) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM recommendation_requests WHERE request_id = %s",
                    (race_headers["Idempotency-Key"],),
                ).fetchone()[0] == 1
            repeated = await client.post(pub_path, headers=admin, json=payload)
            assert repeated.status_code == 200
            assert repeated.json()["replayed"] is True

        restarted = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted),
                                     base_url="http://test") as client:
            assert (await client.get("/health/ready")).status_code == 200
            replayed = await client.post("/api/v1/recommendations", headers=session_headers,
                                         json=request)
            assert replayed.json() == result.json()
            deactivated = await client.post("/api/v1/admin/items/item-a/deactivate",
                                            headers=admin)
            assert deactivated.status_code == 200
            assert deactivated.json()["exclusion_version"] == 1
            new_result = await client.post("/api/v1/recommendations",
                                           headers={**session_headers,
                                                    "Idempotency-Key": str(uuid4())},
                                           json=request)
            assert new_result.status_code == 200
            assert [item["item_id"] for item in new_result.json()["items"]] == ["item-b"]

    asyncio.run(exercise())


def test_publication_recovers_both_sides_of_pointer_commit(isolated_database, monkeypatch, tmp_path):
    monkeypatch.setenv("EVOREC_ADMIN_TOKEN", "local-admin-test-token-32-characters")
    managed, first, _ = _bundle(tmp_path)
    _, second, _ = _bundle(tmp_path)
    monkeypatch.setenv("EVOREC_BUNDLE_ROOT", str(managed))

    async def exercise():
        app = create_app()
        admin = {"X-Admin-Token": "local-admin-test-token-32-characters"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            items = [{"item_id": item, "title": item, "category": "test"}
                     for item in ("item-a", "item-b")]
            await client.post("/api/v1/admin/catalog/imports", headers=admin,
                              json={"batch_id": str(uuid4()), "items": items})
            for bundle in (first, second):
                result = await client.post(f"/api/v1/admin/bundles/{bundle.name}/register",
                                           headers=admin)
                assert result.status_code == 200
            result = await client.post(
                f"/api/v1/admin/bundles/{first.name}/publish", headers=admin,
                json={"operation_id": str(uuid4()), "expected_active_bundle_id": None},
            )
            assert result.status_code == 200
            wrong_expected = await client.post(
                f"/api/v1/admin/bundles/{second.name}/publish", headers=admin,
                json={"operation_id": str(uuid4()), "expected_active_bundle_id": None},
            )
            assert wrong_expected.status_code == 409
            assert (await client.get("/health/ready")).status_code == 200

            with psycopg.connect(isolated_database) as connection:
                removed = connection.execute(
                    "DELETE FROM bundle_items WHERE bundle_id = %s AND internal_item_id = 0 "
                    "RETURNING item_id, internal_item_id", (second.name,),
                ).fetchone()
            changed = await client.post(
                f"/api/v1/admin/bundles/{second.name}/publish", headers=admin,
                json={"operation_id": str(uuid4()), "expected_active_bundle_id": first.name},
            )
            assert changed.status_code == 409
            assert changed.json()["error"]["code"] == "bundle_members_changed"
            with psycopg.connect(isolated_database) as connection:
                connection.execute(
                    "INSERT INTO bundle_items (bundle_id, item_id, internal_item_id) "
                    "VALUES (%s, %s, %s)", (second.name, removed[0], removed[1]),
                )

        before_id = uuid4()
        with psycopg.connect(isolated_database) as connection:
            connection.execute(
                "INSERT INTO publication_operations "
                "(operation_id, target_bundle_id, expected_active_bundle_id, "
                "previous_bundle_id, status) VALUES (%s, %s, %s, %s, 'preparing')",
                (before_id, second.name, first.name, first.name),
            )
            connection.execute("UPDATE catalog_control SET admission_open = false")
        recovering = create_app()
        async with recovering.router.lifespan_context(recovering):
            with psycopg.connect(isolated_database) as connection:
                assert connection.execute(
                    "SELECT status FROM publication_operations WHERE operation_id = %s",
                    (before_id,),
                ).fetchone()[0] == "aborted"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=recovering),
                                         base_url="http://test") as client:
                assert (await client.get("/health/ready")).status_code == 200
                state = (await client.get("/api/v1/admin/publication", headers=admin)).json()
                assert state["active_bundle_id"] == first.name
                assert state["admission_open"] is True
                aborted_retry = await client.post(
                    f"/api/v1/admin/bundles/{second.name}/publish", headers=admin,
                    json={"operation_id": str(before_id),
                          "expected_active_bundle_id": first.name},
                )
                assert aborted_retry.status_code == 409
                assert aborted_retry.json()["error"]["code"] == "publication_aborted"
        with psycopg.connect(isolated_database) as connection:
            assert connection.execute(
                "SELECT status FROM publication_operations WHERE operation_id = %s",
                (before_id,),
            ).fetchone()[0] == "aborted"

        after_id = uuid4()
        with psycopg.connect(isolated_database) as connection:
            connection.execute(
                "INSERT INTO publication_operations "
                "(operation_id, target_bundle_id, expected_active_bundle_id, "
                "previous_bundle_id, status) VALUES (%s, %s, %s, %s, 'switched')",
                (after_id, second.name, first.name, first.name),
            )
            connection.execute(
                "UPDATE bundle_versions SET status = 'retired' WHERE bundle_id = %s",
                (first.name,),
            )
            connection.execute(
                "UPDATE bundle_versions SET status = 'active' WHERE bundle_id = %s",
                (second.name,),
            )
            connection.execute(
                "UPDATE catalog_control SET active_bundle_id = %s, admission_open = false",
                (second.name,),
            )
        restarted = create_app()
        async with restarted.router.lifespan_context(restarted):
            with psycopg.connect(isolated_database) as connection:
                assert connection.execute(
                    "SELECT status FROM publication_operations WHERE operation_id = %s",
                    (after_id,),
                ).fetchone()[0] == "completed"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted),
                                         base_url="http://test") as client:
                assert (await client.get("/health/ready")).status_code == 200
                state = (await client.get("/api/v1/admin/publication", headers=admin)).json()
                assert state["active_bundle_id"] == second.name
                assert state["admission_open"] is True
        with psycopg.connect(isolated_database) as connection:
            assert connection.execute(
                "SELECT status FROM publication_operations WHERE operation_id = %s",
                (after_id,),
            ).fetchone()[0] == "completed"

    asyncio.run(exercise())


def test_failed_activation_stays_closed_until_artifact_is_restored(
    isolated_database, monkeypatch, tmp_path,
):
    managed, bundle, _ = _bundle(tmp_path)
    monkeypatch.setenv("EVOREC_BUNDLE_ROOT", str(managed))
    monkeypatch.setenv("EVOREC_ADMIN_TOKEN", "local-admin-test-token-32-characters")
    demo = build_demo_application()
    assert demo.manager is not None
    admin = {"X-Admin-Token": "local-admin-test-token-32-characters"}
    operation_id = uuid4()

    async def exercise():
        app = create_app(demo_application=demo)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            imported = await client.post(
                "/api/v1/admin/catalog/imports", headers=admin,
                json={"batch_id": str(uuid4()), "items": [
                    {"item_id": item, "title": item, "category": "test"}
                    for item in ("item-a", "item-b")
                ]},
            )
            assert imported.status_code == 200
            registered = await client.post(
                f"/api/v1/admin/bundles/{bundle.name}/register", headers=admin,
            )
            assert registered.status_code == 200

        def fail_activation(_):
            raise RuntimeError("injected failure after pointer commit")

        monkeypatch.setattr(demo.backend, "activate_runtime", fail_activation)
        with pytest.raises(RuntimeError, match="injected failure"):
            demo.manager.publish(operation_id, UUID(bundle.name), None)
        with psycopg.connect(isolated_database) as connection:
            control = connection.execute(
                "SELECT active_bundle_id, admission_open FROM catalog_control"
            ).fetchone()
            status = connection.execute(
                "SELECT status FROM publication_operations WHERE operation_id = %s",
                (operation_id,),
            ).fetchone()[0]
        assert str(control[0]) == bundle.name
        assert control[1] is False
        assert status == "switched"

        missing = bundle.with_name(f"{bundle.name}-temporarily-unavailable")
        bundle.rename(missing)
        try:
            unavailable = create_app()
            async with unavailable.router.lifespan_context(unavailable):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=unavailable),
                                             base_url="http://test") as client:
                    ready = await client.get("/health/ready")
                    assert ready.status_code == 503
                    assert "publication_barrier_closed" in ready.json()["blockers"]
            with psycopg.connect(isolated_database) as connection:
                assert connection.execute(
                    "SELECT admission_open FROM catalog_control"
                ).fetchone()[0] is False
        finally:
            missing.rename(bundle)

        recovered = create_app()
        async with recovered.router.lifespan_context(recovered):
            with psycopg.connect(isolated_database) as connection:
                assert connection.execute(
                    "SELECT status FROM publication_operations WHERE operation_id = %s",
                    (operation_id,),
                ).fetchone()[0] == "completed"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=recovered),
                                         base_url="http://test") as client:
                assert (await client.get("/health/ready")).status_code == 200

    asyncio.run(exercise())


def test_concurrent_publications_do_not_overwrite_expected_version(
    isolated_database, monkeypatch, tmp_path,
):
    managed, first, _ = _bundle(tmp_path)
    _, second, _ = _bundle(tmp_path)
    monkeypatch.setenv("EVOREC_BUNDLE_ROOT", str(managed))
    monkeypatch.setenv("EVOREC_ADMIN_TOKEN", "local-admin-test-token-32-characters")
    admin = {"X-Admin-Token": "local-admin-test-token-32-characters"}

    async def exercise():
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            imported = await client.post(
                "/api/v1/admin/catalog/imports", headers=admin,
                json={"batch_id": str(uuid4()), "items": [
                    {"item_id": item, "title": item, "category": "test"}
                    for item in ("item-a", "item-b")
                ]},
            )
            assert imported.status_code == 200
            for bundle in (first, second):
                registered = await client.post(
                    f"/api/v1/admin/bundles/{bundle.name}/register", headers=admin,
                )
                assert registered.status_code == 200
            responses = await asyncio.gather(*(
                client.post(
                    f"/api/v1/admin/bundles/{bundle.name}/publish", headers=admin,
                    json={"operation_id": str(uuid4()),
                          "expected_active_bundle_id": None},
                ) for bundle in (first, second)
            ))
            assert sorted(response.status_code for response in responses) == [200, 409]
            loser = next(response for response in responses if response.status_code == 409)
            assert loser.json()["error"]["code"] == "publication_version_conflict"
            winner = next(response for response in responses if response.status_code == 200)
            state = (await client.get("/api/v1/admin/publication", headers=admin)).json()
            assert state["active_bundle_id"] == winner.json()["active_bundle_id"]
            assert state["admission_open"] is True
        with psycopg.connect(isolated_database) as connection:
            assert connection.execute(
                "SELECT count(*) FROM publication_operations WHERE status = 'completed'"
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT count(*) FROM bundle_versions WHERE status = 'active'"
            ).fetchone()[0] == 1

    asyncio.run(exercise())
