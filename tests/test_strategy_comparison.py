"""A comparison preview shares one snapshot and never creates a recommendation record."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import httpx
import psycopg
import pytest

from evorec.api.app import create_app
from evorec.application.compare import CompareStrategies, ComparisonCommand
from evorec.bootstrap import build_demo_application
from evorec.contracts import CatalogImportInput
from evorec.domain.models import CatalogSnapshot, RankedBatch, ScoredCandidate, Strategy
from evorec.infrastructure.memory import InMemoryDemoBackend


def published_application(monkeypatch, tmp_path):
    monkeypatch.setenv("EVOREC_BUNDLE_ROOT", str(tmp_path / "bundles"))
    application = build_demo_application()
    batch = uuid4()
    application.manager.import_items(CatalogImportInput(batch_id=batch, items=[
        {"item_id": "alpha", "title": "Puzzle A", "category": "puzzle"},
        {"item_id": "beta", "title": "Puzzle B", "category": "puzzle"},
    ]))
    build = application.manager.builds.process(batch, uuid4())
    application.manager.builds.publish(build["build_id"], uuid4())
    return application, build


def test_memory_preview_reports_real_fallback_without_recording_recommendations():
    backend = InMemoryDemoBackend()
    app = create_app(demo_application=build_demo_application(backend))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            session = (await client.post("/api/v1/sessions")).json()
            path = "/api/v1/strategy-comparisons/preview"
            payload = {"session_id": session["session_id"], "expected_history_version": 0,
                       "strategies": ["popular", "dense", "adaptive"], "k": 3}
            assert (await client.post(path, json=payload)).status_code == 401
            assert (await client.post(path, json=payload,
                                      headers={"X-Session-Token": "wrong"})).status_code == 401
            response = await client.post(path, json=payload, headers={
                "X-Session-Token": session["access_token"],
            })
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["persisted"] is False
            assert result["history_version"] == 0
            assert [entry["actual_strategy"] for entry in result["strategies"]] == [
                "popular", "popular", "popular",
            ]
            assert result["strategies"][1]["fallback_reason"] == "strategy_not_loaded_in_memory_demo"
            assert len(result["common_item_ids"]) == 3
            assert all(entry["unique_item_ids"] == [] for entry in result["strategies"])
            assert all(entry["elapsed_ms"] >= 0 for entry in result["strategies"])
            assert not backend._request_states
            assert (await client.post('/api/v1/strategy-comparisons', json=payload, headers={
                'X-Session-Token': session['access_token'],
            })).status_code == 503
            assert (await client.post('/api/v1/strategy-comparison-jobs', json=payload, headers={
                'X-Session-Token': session['access_token'],
            })).status_code == 503
            assert (await client.post(path, json={**payload, "strategies": ["popular", "popular"]},
                                      headers={"X-Session-Token": session["access_token"]})).status_code == 422
            assert (await client.post(path, json={**payload, "strategies": ["popular", "generative"]},
                                      headers={"X-Session-Token": session["access_token"]})).status_code == 422
            assert (await client.post(path, json={**payload, "expected_history_version": 1},
                                      headers={"X-Session-Token": session["access_token"]})).status_code == 409

    asyncio.run(exercise())


def test_preview_keeps_original_session_and_catalog_when_live_state_changes():
    backend = InMemoryDemoBackend()

    async def exercise():
        created = await backend.create_session()
        original_bundle = backend.catalog.bundle_id
        first, shared, second, third = sorted(backend.catalog.eligible_items)[:4]
        candidate_ids = ((first, shared), (shared, second), (shared, third))
        calls = 0

        async def changing_rank(context, command):
            nonlocal calls
            ids = candidate_ids[calls]
            calls += 1
            if calls == 1:
                backend.catalog = CatalogSnapshot(uuid4(), 1, frozenset({"replacement"}))
                backend._sessions[created.snapshot.session_id] = replace(
                    created.snapshot, history_version=1, hidden_items=frozenset(ids),
                )
            return RankedBatch(context.binding, Strategy.POPULAR, tuple(
                ScoredCandidate(item_id, 1.0, "test") for item_id in ids
            ), "deliberate_test_fallback" if command.strategy == Strategy.DENSE else None)

        backend.rank = changing_rank
        command = ComparisonCommand(uuid4(), created.snapshot.session_id, created.access_token,
                                    0, (Strategy.POPULAR, Strategy.DENSE, Strategy.ADAPTIVE), 2)
        preview = await CompareStrategies(backend, backend).preview(command)
        assert calls == 3
        assert preview.binding.bundle_id == original_bundle
        assert preview.binding.history_version == 0
        assert preview.common_item_ids == (shared,)
        assert [entry.unique_item_ids for entry in preview.strategies] == [
            (first,), (second,), (third,),
        ]
        assert [[item.item_id for item in entry.items] for entry in preview.strategies] == [
            [first, shared], [shared, second], [shared, third],
        ]
        assert not backend._request_states

    asyncio.run(exercise())


def test_postgres_preview_uses_published_bundle_without_normal_request_writes(
    isolated_database, monkeypatch, tmp_path,
):
    application, build = published_application(monkeypatch, tmp_path)
    app = create_app(demo_application=application)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            session = (await client.post("/api/v1/sessions")).json()
            response = await client.post("/api/v1/strategy-comparisons/preview", headers={
                "X-Session-Token": session["access_token"],
            }, json={"session_id": session["session_id"], "expected_history_version": 0,
                     "strategies": ["popular", "dense"], "k": 2})
            assert response.status_code == 200, response.text
            preview = response.json()
            assert preview["bundle_id"] == str(build["bundle_id"])
            assert preview["strategies"][0]["actual_strategy"] == "popular"
            assert preview["strategies"][1]["actual_strategy"] == "dense"
            assert preview["strategies"][1]["fallback_reason"] is None
            assert set(preview["common_item_ids"]) == {"alpha", "beta"}
            assert preview["persisted"] is False

    asyncio.run(exercise())
    with psycopg.connect(isolated_database) as connection:
        assert connection.execute("SELECT count(*) FROM recommendation_requests").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM request_items").fetchone()[0] == 0


def test_saved_comparison_survives_restart_reset_and_catalog_changes(
    isolated_database, monkeypatch, tmp_path,
):
    application, build = published_application(monkeypatch, tmp_path)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=application)),
                                     base_url="http://test") as client:
            session = (await client.post("/api/v1/sessions")).json()
            headers = {"X-Session-Token": session["access_token"], "Idempotency-Key": str(uuid4())}
            payload = {"session_id": session["session_id"], "expected_history_version": 0,
                       "strategies": ["popular", "dense"], "k": 2}
            path = "/api/v1/strategy-comparisons"
            assert (await client.post(path, json=payload)).status_code == 401
            result = await client.post(path, json=payload, headers=headers)
            assert result.status_code == 200, result.text
            saved = result.json()
            assert saved["persisted"] is True and saved["status"] == "completed"
            assert saved["comparison_id"] == headers["Idempotency-Key"]
            assert saved["bundle_id"] == str(build["bundle_id"])
            assert saved["input_snapshot"]["eligible_items"] == ["alpha", "beta"]
            assert saved["requested_k"] == 2
            assert (await client.post(path, json={**payload, "k": 1}, headers=headers)).status_code == 409
            assert (await client.post(path, json=payload, headers={
                **headers, "Idempotency-Key": "invalid",
            })).status_code == 422
            await client.post(f'/api/v1/sessions/{session["session_id"]}/reset', headers=headers)
            with psycopg.connect(isolated_database) as connection:
                connection.execute("UPDATE catalog_control SET admission_open = false")
                connection.execute("UPDATE items SET is_active = false")

        # New application/runtime objects cannot rely on any process-local result cache.
        restarted = build_demo_application()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=restarted)),
                                     base_url="http://test") as client:
            replay = await client.post(path, json=payload, headers=headers)
            assert replay.status_code == 200, replay.text
            assert replay.json() == saved
            query = f'{path}/{saved["comparison_id"]}?session_id={session["session_id"]}'
            assert (await client.get(query, headers=headers)).json() == saved
            assert (await client.get(query)).status_code == 401
            assert (await client.get(query, headers={"X-Session-Token": "wrong"})).status_code == 401
            other = (await client.post("/api/v1/sessions")).json()
            other_headers = {"X-Session-Token": other["access_token"]}
            assert (await client.get(f'{path}/{saved["comparison_id"]}?session_id={other["session_id"]}',
                                     headers=other_headers)).status_code == 404
            assert (await client.post(path, json={**payload, "session_id": other["session_id"]},
                                      headers={**other_headers, "Idempotency-Key": headers["Idempotency-Key"]})).status_code == 409

    asyncio.run(exercise())
    with psycopg.connect(isolated_database) as connection:
        assert connection.execute("SELECT count(*) FROM strategy_comparisons").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM recommendation_requests").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["concurrent", "lost_commit_response", "ranking_error"])
def test_saved_comparison_atomicity_and_retry(isolated_database, monkeypatch, tmp_path, failure):
    application, _ = published_application(monkeypatch, tmp_path)
    original_rank = application.backend.rank
    original_save = application.compare.records._save
    gate = asyncio.Event()
    arrivals = 0

    async def rank(context, command):
        nonlocal arrivals
        if failure == "ranking_error":
            raise RuntimeError("test ranking interruption")
        if failure == "concurrent" and command.strategy == Strategy.POPULAR:
            arrivals += 1
            if arrivals == 2:
                gate.set()
            await gate.wait()
        return await original_rank(context, command)

    def save_with_lost_response(command, result):
        original_save(command, result)
        raise psycopg.OperationalError("test response lost after commit")

    monkeypatch.setattr(application.backend, "rank", rank)
    if failure == "lost_commit_response":
        monkeypatch.setattr(application.compare.records, "_save", save_with_lost_response)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=application)),
                                     base_url="http://test") as client:
            session = (await client.post("/api/v1/sessions")).json()
            headers = {"X-Session-Token": session["access_token"], "Idempotency-Key": str(uuid4())}
            payload = {"session_id": session["session_id"], "expected_history_version": 0,
                       "strategies": ["popular", "dense"], "k": 2}
            async def post():
                return await client.post("/api/v1/strategy-comparisons", json=payload, headers=headers)
            if failure == "concurrent":
                first, second = await asyncio.gather(post(), post())
                assert first.status_code == second.status_code == 200
                assert first.json() == second.json()
                assert arrivals == 2  # Both computations raced to one durable winner.
            else:
                failed = await post()
                assert failed.status_code == 503
                with psycopg.connect(isolated_database) as connection:
                    count = connection.execute("SELECT count(*) FROM strategy_comparisons").fetchone()[0]
                    assert count == (1 if failure == "lost_commit_response" else 0)
                monkeypatch.setattr(application.backend, "rank", original_rank)
                monkeypatch.setattr(application.compare.records, "_save", original_save)
                retried = await post()
                assert retried.status_code == 200, retried.text
                assert (await post()).json() == retried.json()

    asyncio.run(exercise())
    with psycopg.connect(isolated_database) as connection:
        assert connection.execute("SELECT count(*) FROM strategy_comparisons").fetchone()[0] == 1


def test_comparison_history_is_paginated_stably_and_isolated_by_session(
    isolated_database, monkeypatch, tmp_path,
):
    application, _ = published_application(monkeypatch, tmp_path)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=application)),
                                     base_url="http://test") as client:
            session = (await client.post('/api/v1/sessions')).json()
            headers = {'X-Session-Token': session['access_token']}
            path = f'/api/v1/strategy-comparisons?session_id={session["session_id"]}'
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers={'X-Session-Token': 'wrong'})).status_code == 401
            assert (await client.get(path, headers=headers)).json()['items'] == []
            ids = []
            for k in [1, 2, 3]:
                response = await client.post('/api/v1/strategy-comparisons', headers=headers, json={
                    'session_id': session['session_id'], 'expected_history_version': 0,
                    'strategies': ['popular', 'dense'], 'k': k,
                })
                assert response.status_code == 200, response.text
                ids.append(response.json()['comparison_id'])
            other = (await client.post('/api/v1/sessions')).json()
            assert (await client.post('/api/v1/strategy-comparisons', headers={
                'X-Session-Token': other['access_token'],
            }, json={'session_id': other['session_id'], 'expected_history_version': 0,
                     'strategies': ['popular', 'dense'], 'k': 1})).status_code == 200
            # Equal timestamps must still produce a deterministic page boundary.
            with psycopg.connect(isolated_database) as connection:
                connection.execute("UPDATE strategy_comparisons SET created_at = '2026-10-02T00:00:00Z'")
            first = (await client.get(path + '&limit=2', headers=headers)).json()
            last = (await client.get(path + '&limit=2&offset=2', headers=headers)).json()
            assert first['has_more'] is True and last['has_more'] is False
            rows = first['items'] + last['items']
            assert [row['comparison_id'] for row in rows] == sorted(ids, reverse=True)
            assert all(row['session_id'] == session['session_id'] for row in rows)
            assert all(row['actual_strategies'] == ['popular', 'dense'] for row in rows)
            assert all('input_snapshot' not in row for row in rows)
            assert (await client.get(path + '&offset=3', headers=headers)).json()['items'] == []
            assert (await client.get(path + '&limit=0', headers=headers)).status_code == 422
            assert (await client.get(path + '&offset=-1', headers=headers)).status_code == 422
            assert (await client.get(path + '&offset=10001', headers=headers)).status_code == 422

    asyncio.run(exercise())
