import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest

from evorec.api.app import create_app
from evorec.domain.errors import SnapshotMismatch
from evorec.domain.models import (
    RecommendationResult,
    RequestBinding,
    ScoredCandidate,
    Strategy,
)
from evorec.infrastructure.postgres import PostgresDemoBackend
from scripts.seed_demo_catalog import main as seed_demo_catalog


def test_postgres_session_and_recommendation_survive_new_app_instance(
    isolated_database, monkeypatch,
):
    monkeypatch.delenv("EVOREC_BUNDLE_ROOT", raising=False)
    seed_demo_catalog()
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
            replay_headers = {**headers, "Idempotency-Key": result["request_id"]}
            repeated = await client.post(
                "/api/v1/recommendations", headers=replay_headers,
                json={"session_id": session["session_id"], "expected_history_version": 0,
                      "strategy": "dense", "k": 2},
            )
            assert repeated.status_code == 200
            assert repeated.json() == result
            detail = {
                "event_id": str(uuid4()),
                "session_id": session["session_id"],
                "request_id": result["request_id"],
                "item_id": result["items"][0]["item_id"],
                "kind": "detail_view",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            detail_recorded = await client.post(
                "/api/v1/feedback", headers=headers, json=detail,
            )
            detail_replayed = await client.post(
                "/api/v1/feedback", headers=headers, json=detail,
            )
            assert detail_recorded.status_code == detail_replayed.status_code == 200
            assert detail_recorded.json()["history_version"] == 1
            assert detail_recorded.json()["exposure_event_id"]
            assert (
                detail_replayed.json()["exposure_event_id"]
                == detail_recorded.json()["exposure_event_id"]
            )
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
            assert recorded.json()["history_version"] == 2
            assert recorded.json()["replayed"] is False
            assert replayed.json()["replayed"] is True
            assert repeated_state.status_code == 200
            assert repeated_state.json()["history_version"] == 2
            assert repeated_state.json()["replayed"] is False
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "feedback_idempotency_conflict"

            persisted_result = RecommendationResult(
                binding=RequestBinding(
                    UUID(result["request_id"]),
                    UUID(result["session_id"]),
                    result["session_epoch"],
                    result["history_version"],
                    UUID(result["bundle_id"]),
                    result["exclusion_version"],
                ),
                requested_strategy=Strategy(result["requested_strategy"]),
                actual_strategy=Strategy(result["actual_strategy"]),
                items=tuple(ScoredCandidate(**item) for item in result["items"]),
                fallback_reason=result["fallback_reason"],
            )
            recorder = PostgresDemoBackend(isolated_database)
            await recorder.save(persisted_result)
            changed_item = replace(
                persisted_result.items[0], score=persisted_result.items[0].score + 0.01,
            )
            with pytest.raises(SnapshotMismatch, match="different result content"):
                await recorder.save(
                    replace(
                        persisted_result,
                        items=(changed_item, *persisted_result.items[1:]),
                    )
                )

        second_app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_app), base_url="http://test"
        ) as client:
            restored = await client.get(
                f"/api/v1/sessions/{session['session_id']}", headers=headers,
            )
            assert restored.status_code == 200
            assert restored.json()["history_version"] == 2
            assert restored.json()["history"] == [detail["item_id"]]
            assert restored.json()["hidden_items"] == [feedback["item_id"]]
            denied = await client.get(
                f"/api/v1/sessions/{session['session_id']}",
                headers={"X-Session-Token": "wrong"},
            )
            assert denied.status_code == 401

        with psycopg.connect(isolated_database) as connection:
            row = connection.execute(
                """
                SELECT status, actual_strategy,
                       (SELECT count(*) FROM request_items WHERE request_id = r.request_id),
                       (SELECT count(*) FROM feedback_events WHERE session_id = %s),
                       (SELECT is_hidden FROM session_item_states
                        WHERE session_id = %s AND item_id = %s),
                       (SELECT event_kind FROM feedback_events WHERE event_id = %s),
                       (SELECT evidence->>'source' FROM feedback_events WHERE event_id = %s)
                FROM recommendation_requests r WHERE request_id = %s
                """,
                (
                    session["session_id"],
                    session["session_id"],
                    feedback["item_id"],
                    detail_recorded.json()["exposure_event_id"],
                    detail_recorded.json()["exposure_event_id"],
                    result["request_id"],
                ),
            ).fetchone()
            assert row == (
                "completed", "popular", 2, 4, True, "exposure", "detail_view_backfill",
            )

    asyncio.run(exercise())
