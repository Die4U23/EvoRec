"""End-to-end V0.1 contracts against both storage implementations."""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from evorec.api.app import create_app
from scripts.seed_demo_catalog import main as seed_demo_catalog


@pytest.mark.parametrize("persistent", [False, True])
def test_sample_feedback_undo_and_reset(persistent, isolated_database, monkeypatch):
    if persistent:
        seed_demo_catalog()
    else:
        monkeypatch.delenv("EVOREC_DATABASE_URL", raising=False)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            created = await client.post("/api/v1/sessions", json={"profile_id": "sample"})
            assert created.status_code == 201, created.text
            session = created.json()
            assert session["history"] == ["demo-coop"]
            assert session["profile_id"] == "sample"
            headers = {"X-Session-Token": session["access_token"]}
            first = await client.post("/api/v1/recommendations", headers=headers, json={
                "session_id": session["session_id"], "expected_history_version": 0,
                "strategy": "popular", "k": 10,
            })
            assert first.status_code == 200, first.text
            recommendation = first.json()
            assert len(recommendation["items"]) == 2
            ids = {item["item_id"] for item in recommendation["items"]}
            assert "demo-coop" not in ids
            item_id = next(iter(ids))
            other_id = next(iter(ids - {item_id}))
            item = await client.get(f"/api/v1/items/{item_id}")
            assert item.status_code == 200 and item.json()["title"]

            async def feedback(kind, desired=None, target=None):
                payload = {"event_id": str(uuid4()), "session_id": session["session_id"],
                           "request_id": recommendation["request_id"], "item_id": target or item_id,
                           "kind": kind, "observed_at": datetime.now(timezone.utc).isoformat()}
                if desired is not None:
                    payload["desired_state"] = desired
                response = await client.post("/api/v1/feedback", headers=headers, json=payload)
                assert response.status_code == 200, response.text
                replay = await client.post("/api/v1/feedback", headers=headers, json=payload)
                assert replay.status_code == 200 and replay.json()["replayed"] is True
                return response.json()

            await feedback("favorite_set", True)
            state = (await client.get(f"/api/v1/sessions/{session['session_id']}", headers=headers)).json()
            assert state["favorite_items"] == [item_id]
            assert state["history"] == ["demo-coop", item_id]
            await feedback("favorite_set", False)
            assert (await client.get(f"/api/v1/sessions/{session['session_id']}", headers=headers)).json()["favorite_items"] == []
            await feedback("hide_set", True, other_id)
            state = (await client.get(f"/api/v1/sessions/{session['session_id']}", headers=headers)).json()
            assert state["hidden_items"] == [other_id]
            after_hide = await client.post("/api/v1/recommendations", headers=headers, json={
                "session_id": session["session_id"], "expected_history_version": state["history_version"],
                "strategy": "popular", "k": 10,
            })
            assert after_hide.status_code == 200 and after_hide.json()["items"] == []
            await feedback("hide_set", False, other_id)
            state = (await client.get(f"/api/v1/sessions/{session['session_id']}", headers=headers)).json()
            assert state["hidden_items"] == []
            after_undo = await client.post("/api/v1/recommendations", headers=headers, json={
                "session_id": session["session_id"], "expected_history_version": state["history_version"],
                "strategy": "popular", "k": 10,
            })
            assert [item["item_id"] for item in after_undo.json()["items"]] == [other_id]
            reset = await client.post(f"/api/v1/sessions/{session['session_id']}/reset", headers=headers)
            assert reset.status_code == 200
            assert reset.json()["history"] == ["demo-coop"]
            assert reset.json()["favorite_items"] == reset.json()["hidden_items"] == []
            assert reset.json()["epoch"] == 1
            stale = await client.post("/api/v1/feedback", headers=headers, json={
                "event_id": str(uuid4()), "session_id": session["session_id"],
                "request_id": recommendation["request_id"], "item_id": item_id,
                "kind": "hide_set", "desired_state": True,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            })
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "session_epoch_conflict"

    asyncio.run(exercise())


def test_new_user_starts_empty_and_missing_item_is_404(monkeypatch):
    monkeypatch.delenv("EVOREC_DATABASE_URL", raising=False)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            session = (await client.post("/api/v1/sessions", json={"profile_id": "new"})).json()
            assert session["history"] == session["favorite_items"] == []
            assert len((await client.get("/api/v1/items")).json()) == 3
            assert (await client.get("/api/v1/items/does-not-exist")).status_code == 404
            assert (await client.post("/api/v1/sessions", json={"profile_id": "unknown"})).status_code == 422

    asyncio.run(exercise())


def test_sample_requires_active_demo_item(isolated_database):
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            rejected = await client.post("/api/v1/sessions", json={"profile_id": "sample"})
            assert rejected.status_code == 409
            assert rejected.json()["error"]["code"] == "sample_profile_unavailable"
            new_user = await client.post("/api/v1/sessions")
            assert new_user.status_code == 201
            assert new_user.json()["profile_id"] == "new"

    asyncio.run(exercise())
