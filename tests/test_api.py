import asyncio

import httpx

from evorec.api.app import create_app
from evorec.application.health import ReadinessQuery
from evorec.domain.models import ReadinessReport


def request(method, path, **kwargs):
    async def call():
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    # M0 has no lifespan-managed resources; persistence integration will need startup fixtures.
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
    assert system["phase"] == "M0-framework"
    for capability in ["recommendations", "catalog_publication", "persistence", "model_training", "frontend"]:
        assert system["capabilities"][capability] is False
    assert request("POST", "/api/v1/recommendations", json={}).status_code == 404
    assert request("POST", "/api/v1/admin/catalog/imports", json={}).status_code == 404


def test_openapi_describes_actual_routes_and_503_readiness():
    schema = request("GET", "/openapi.json").json()
    assert set(schema["paths"]) == {"/health/live", "/health/ready", "/api/v1/system"}
    responses = schema["paths"]["/health/ready"]["get"]["responses"]
    assert "503" in responses
    assert "200" in responses  # Available for a future real readiness adapter; M0 still returns 503.


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
