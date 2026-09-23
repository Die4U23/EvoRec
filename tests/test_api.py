import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from evorec.api.app import create_app
from evorec.application.health import ReadinessQuery
from evorec.bootstrap import build_demo_application
from evorec.domain.models import ReadinessReport
from evorec.infrastructure.memory import InMemoryDemoBackend


def memory_app():
    return create_app(demo_application=build_demo_application(InMemoryDemoBackend()))


def request(method, path, **kwargs):
    async def call():
        transport = httpx.ASGITransport(app=memory_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    # The memory demo has no lifespan-managed resources; persistence will need startup fixtures.
    return asyncio.run(call())


def test_liveness_does_not_claim_business_readiness():
    assert request("GET", "/health/live").status_code == 200
    ready = request("GET", "/health/ready")
    assert ready.status_code == 503
    assert ready.json()["ready"] is False
    assert set(ready.json()["blockers"]) == {
        "database_not_connected", "model_runtime_not_loaded", "catalog_bundle_not_loaded"
    }


def test_unimplemented_capabilities_are_not_advertised_as_available():
    system = request("GET", "/api/v1/system").json()
    assert system["phase"] == "M1-persistent-demo"
    assert system["capabilities"]["recommendations"] is True
    for capability in ["catalog_publication", "persistence", "model_training", "frontend"]:
        assert system["capabilities"][capability] is False
    assert request("POST", "/api/v1/admin/catalog/imports", json={}).status_code == 404


def test_openapi_describes_actual_routes_and_503_readiness():
    schema = request("GET", "/openapi.json").json()
    assert set(schema["paths"]) == {
        "/health/live", "/health/ready", "/api/v1/system",
        "/api/v1/sessions", "/api/v1/sessions/{session_id}",
        "/api/v1/sessions/{session_id}/reset", "/api/v1/recommendations",
        "/api/v1/feedback",
    }
    responses = schema["paths"]["/health/ready"]["get"]["responses"]
    assert "503" in responses
    assert "200" in responses  # Available for a future real readiness adapter; default remains 503.


def test_readiness_uses_current_probe_state_instead_of_a_startup_constant():
    class MutableProbe:
        blockers = ()

        async def check(self):
            return ReadinessReport(self.blockers)

    async def exercise():
        probe = MutableProbe()
        app = create_app(readiness_query=ReadinessQuery(probe))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            ready = await client.get("/health/ready")
            assert ready.status_code == 200
            assert ready.json() == {"ready": True, "blockers": []}
            probe.blockers = ("publication_barrier_closed",)
            unavailable = await client.get("/health/ready")
            assert unavailable.status_code == 503
            assert unavailable.json()["blockers"] == ["publication_barrier_closed"]

    asyncio.run(exercise())


def test_memory_demo_session_recommendation_and_stale_history_conflict():
    async def exercise():
        app = memory_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/v1/sessions")
            assert created.status_code == 201
            session = created.json()
            assert session["epoch"] == session["history_version"] == 0
            headers = {"X-Session-Token": session["access_token"]}

            recommended = await client.post("/api/v1/recommendations", json={
                "session_id": session["session_id"],
                "expected_history_version": 0,
                "strategy": "dense",
                "k": 2,
            }, headers=headers)
            assert recommended.status_code == 200
            result = recommended.json()
            assert result["requested_strategy"] == "dense"
            assert result["actual_strategy"] == "popular"
            assert result["fallback_reason"] == "strategy_not_loaded_in_memory_demo"
            assert [item["item_id"] for item in result["items"]] == ["demo-coop", "demo-racing"]

            reset = await client.post(
                f"/api/v1/sessions/{session['session_id']}/reset", headers=headers,
            )
            assert reset.status_code == 200
            assert reset.json()["epoch"] == reset.json()["history_version"] == 1

            stale = await client.post("/api/v1/recommendations", json={
                "session_id": session["session_id"],
                "expected_history_version": 0,
                "strategy": "popular",
                "k": 1,
            }, headers=headers)
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "history_conflict"
            assert stale.json()["error"]["request_id"]

    asyncio.run(exercise())


def test_memory_demo_missing_session_uses_the_business_error_shape():
    missing = request(
        "GET", "/api/v1/sessions/00000000-0000-0000-0000-000000000000",
        headers={"X-Session-Token": "unknown-token"},
    )
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {
            "code": "session_not_found",
            "message": "session does not exist",
            "retryable": False,
            "request_id": None,
        }
    }


def test_memory_demo_requires_and_checks_session_token():
    async def exercise():
        app = memory_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            session = (await client.post("/api/v1/sessions")).json()
            path = f"/api/v1/sessions/{session['session_id']}"
            missing = await client.get(path)
            invalid = await client.get(path, headers={"X-Session-Token": "wrong"})
            valid = await client.get(path, headers={"X-Session-Token": session["access_token"]})
            assert missing.status_code == invalid.status_code == 401
            assert valid.status_code == 200
            assert valid.json()["session_id"] == session["session_id"]

    asyncio.run(exercise())


def test_memory_feedback_is_idempotent_and_rejects_stale_epoch():
    async def exercise():
        app = memory_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            session = (await client.post("/api/v1/sessions")).json()
            headers = {"X-Session-Token": session["access_token"]}
            recommendation = (
                await client.post(
                    "/api/v1/recommendations",
                    headers=headers,
                    json={
                        "session_id": session["session_id"],
                        "expected_history_version": 0,
                        "strategy": "popular",
                        "k": 1,
                    },
                )
            ).json()
            feedback = {
                "event_id": str(uuid4()),
                "session_id": session["session_id"],
                "request_id": recommendation["request_id"],
                "item_id": recommendation["items"][0]["item_id"],
                "kind": "detail_view",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            first = await client.post("/api/v1/feedback", headers=headers, json=feedback)
            replay = await client.post("/api/v1/feedback", headers=headers, json=feedback)
            changed = await client.post(
                "/api/v1/feedback",
                headers=headers,
                json={**feedback, "observed_at": "2026-09-23T01:02:03+00:00"},
            )
            assert first.status_code == replay.status_code == 200
            assert first.json()["history_version"] == 1
            assert first.json()["replayed"] is False
            assert replay.json()["replayed"] is True
            assert changed.status_code == 409
            assert changed.json()["error"]["code"] == "feedback_idempotency_conflict"

            current = await client.get(
                f"/api/v1/sessions/{session['session_id']}", headers=headers,
            )
            assert current.json()["history"] == [feedback["item_id"]]
            await client.post(
                f"/api/v1/sessions/{session['session_id']}/reset", headers=headers,
            )
            stale = await client.post(
                "/api/v1/feedback",
                headers=headers,
                json={**feedback, "event_id": str(uuid4())},
            )
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "session_epoch_conflict"

    asyncio.run(exercise())
