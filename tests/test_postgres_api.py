import asyncio
from datetime import datetime, timezone
import os
from uuid import uuid4

import httpx
import psycopg
import pytest

from evorec.api.app import create_app


DATABASE_URL = os.getenv("EVOREC_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="EVOREC_DATABASE_URL is not configured")


def test_postgres_session_and_recommendation_survive_new_app_instance():
    async def exercise():
        first_app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first_app), base_url="http://test"
        ) as client:
            system = (await client.get("/api/v1/system")).json()
            assert system["capabilities"]["persistence"] is True
            created = await client.post("/api/v1/sessions")
            assert created.status_code == 201
            session = created.json()
            headers = {"X-Session-Token": session["access_token"]}
            recommended = await client.post(
                "/api/v1/recommendations",
                headers=headers,
                json={
                    "session_id": session["session_id"],
                    "expected_history_version": 0,
                    "strategy": "dense",
                    "k": 2,
                },
            )
            assert recommended.status_code == 200
            result = recommended.json()
            assert result["actual_strategy"] == "popular"
            assert result["fallback_reason"] == "strategy_not_loaded_in_postgres_demo"
            assert len(result["items"]) == 2
            feedback = {
                "event_id": str(uuid4()),
                "session_id": session["session_id"],
                "request_id": result["request_id"],
                "item_id": result["items"][0]["item_id"],
                "kind": "hide_set",
                "desired_state": True,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            recorded = await client.post("/api/v1/feedback", headers=headers, json=feedback)
            replayed = await client.post("/api/v1/feedback", headers=headers, json=feedback)
            repeated_state = await client.post(
                "/api/v1/feedback",
                headers=headers,
                json={**feedback, "event_id": str(uuid4())},
            )
            conflict = await client.post(
                "/api/v1/feedback",
                headers=headers,
                json={**feedback, "desired_state": False},
            )
            assert recorded.status_code == replayed.status_code == 200
            assert recorded.json()["history_version"] == 1
            assert recorded.json()["replayed"] is False
            assert replayed.json()["replayed"] is True
            assert repeated_state.status_code == 200
            assert repeated_state.json()["history_version"] == 1
            assert repeated_state.json()["replayed"] is False
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "feedback_idempotency_conflict"

        second_app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_app), base_url="http://test"
        ) as client:
            restored = await client.get(
                f"/api/v1/sessions/{session['session_id']}", headers=headers,
            )
            assert restored.status_code == 200
            assert restored.json()["history_version"] == 1
            assert restored.json()["hidden_items"] == [feedback["item_id"]]
            denied = await client.get(
                f"/api/v1/sessions/{session['session_id']}",
                headers={"X-Session-Token": "wrong"},
            )
            assert denied.status_code == 401

        with psycopg.connect(DATABASE_URL) as connection:
            row = connection.execute(
                """
                SELECT status, actual_strategy,
                       (SELECT count(*) FROM request_items WHERE request_id = r.request_id),
                       (SELECT count(*) FROM feedback_events WHERE session_id = %s),
                       (SELECT is_hidden FROM session_item_states
                        WHERE session_id = %s AND item_id = %s)
                FROM recommendation_requests r WHERE request_id = %s
                """,
                (
                    session["session_id"],
                    session["session_id"],
                    feedback["item_id"],
                    result["request_id"],
                ),
            ).fetchone()
            assert row == ("completed", "popular", 2, 2, True)
            connection.execute(
                "DELETE FROM session_item_states WHERE session_id = %s",
                (session["session_id"],),
            )
            connection.execute(
                "DELETE FROM feedback_events WHERE session_id = %s",
                (session["session_id"],),
            )
            connection.execute(
                "DELETE FROM recommendation_requests WHERE request_id = %s",
                (result["request_id"],),
            )
            connection.execute(
                "DELETE FROM sessions WHERE session_id = %s", (session["session_id"],),
            )

    asyncio.run(exercise())
