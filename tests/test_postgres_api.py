import asyncio
import os

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

        second_app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_app), base_url="http://test"
        ) as client:
            restored = await client.get(
                f"/api/v1/sessions/{session['session_id']}", headers=headers,
            )
            assert restored.status_code == 200
            denied = await client.get(
                f"/api/v1/sessions/{session['session_id']}",
                headers={"X-Session-Token": "wrong"},
            )
            assert denied.status_code == 401

        with psycopg.connect(DATABASE_URL) as connection:
            row = connection.execute(
                """
                SELECT status, actual_strategy,
                       (SELECT count(*) FROM request_items WHERE request_id = r.request_id)
                FROM recommendation_requests r WHERE request_id = %s
                """,
                (result["request_id"],),
            ).fetchone()
            assert row == ("completed", "popular", 2)
            connection.execute(
                "DELETE FROM recommendation_requests WHERE request_id = %s",
                (result["request_id"],),
            )
            connection.execute(
                "DELETE FROM sessions WHERE session_id = %s", (session["session_id"],),
            )

    asyncio.run(exercise())
