"""Synthetic package TCP tests and negative guards; not full-corpus acceptance."""

import asyncio
import json
from pathlib import Path
import struct
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest

from evorec.api.app import create_app
from scripts.run_r06_demo import DemoProcess, main, marker, run, validate
from test_r06_bundle import _build
from test_r06_online import online


def _output():
    return Path(__file__).resolve().parents[1] / "artifacts" / "test-demo" / uuid4().hex


def test_fixed_sample_create_feedback_reset_and_new_profile(online):
    app, _, _ = online
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=app)),
                                     base_url="http://test") as client:
            sample = await client.post("/api/v1/sessions", json={"profile_id": "sample"})
            assert sample.status_code == 201, sample.text
            session = sample.json()
            assert session["history"] == ["a"] and session["profile_id"] == "sample"
            headers = {"X-Session-Token": session["access_token"]}
            body = dict(session_id=session["session_id"], expected_history_version=0, strategy="dense", k=3)
            result = await client.post("/api/v1/recommendations", json=body, headers=headers)
            assert result.status_code == 200, result.text
            assert "a" not in {i["item_id"] for i in result.json()["items"]}
            # Change real stored state, then verify reset is approved seed, not demo-coop.
            with app.backend._connect() as c:
                c.execute("UPDATE sessions SET history='[\"b\"]', history_version=1 WHERE session_id=%s",
                          (session["session_id"],))
            reset = await client.post(f"/api/v1/sessions/{session['session_id']}/reset", headers=headers)
            assert reset.status_code == 200, reset.text
            assert reset.json()["history"] == ["a"] and reset.json()["epoch"] == 1
            assert reset.json()["favorite_items"] == reset.json()["hidden_items"] == []
            new = await client.post("/api/v1/sessions", json={"profile_id": "new"})
            assert new.status_code == 201 and new.json()["history"] == []
    asyncio.run(exercise())


@pytest.mark.parametrize("change", [
    "UPDATE items SET is_active=false WHERE item_id='a'",
    "UPDATE items SET r06_model_text='drift' WHERE item_id='a'",
    "UPDATE items SET r06_first_seen_ms=2 WHERE item_id='a'",
    "DELETE FROM bundle_items WHERE item_id='a'",
    "UPDATE bundle_items SET internal_item_id=99 WHERE item_id='a'",
])
def test_fixed_sample_rejects_drift_without_fallback_or_reset_mutation(online, change):
    app, _, _ = online
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=app)),
                                     base_url="http://test") as client:
            session = (await client.post("/api/v1/sessions", json={"profile_id": "sample"})).json()
            headers = {"X-Session-Token": session["access_token"]}
            path = f"/api/v1/sessions/{session['session_id']}"
            before = (await client.get(path, headers=headers)).json()
            with app.backend._connect() as c:
                c.execute(change)
                count = c.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"]
            rejected = await client.post("/api/v1/sessions", json={"profile_id": "sample"})
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["error"]["code"] == "sample_profile_unavailable"
            denied = await client.post(path + "/reset", headers={"X-Session-Token": "wrong"})
            assert denied.status_code == 401
            reset = await client.post(path + "/reset", headers=headers)
            assert reset.status_code == 409 and reset.json()["error"]["code"] == "sample_profile_unavailable"
            assert (await client.get(path, headers=headers)).json() == before
            with app.backend._connect() as c:
                assert c.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"] == count
            assert (await client.post("/api/v1/sessions", json={"profile_id": "new"})).status_code == 201
    asyncio.run(exercise())


def test_reset_closed_gate_rejects_but_create_recovers_existing_publication(online):
    app, _, _ = online
    async def exercise():
        session = await app.backend.create_session("sample")
        with app.backend._connect() as c:
            c.execute("UPDATE catalog_control SET admission_open=false")
        with pytest.raises(ValueError):
            await app.backend.reset_session(session.snapshot.session_id, session.access_token)
        assert (await app.backend.get_session(session.snapshot.session_id, session.access_token)) == session.snapshot
        recovered = await app.backend.create_session("sample")
        assert recovered.snapshot.history == ("a",)
    asyncio.run(exercise())


@pytest.mark.parametrize("port", [8000, -1, 65536, True])
def test_protected_or_invalid_port_rejected_before_writes(tmp_path, port):
    output = _output()
    with pytest.raises(ValueError):
        validate(output, "host=localhost dbname=test", tmp_path, uuid4(), "a"*64, "stdlib", port)
    assert not output.exists()


@pytest.mark.parametrize("database", ["host=example.com dbname=test",
                                     "host=localhost hostaddr=203.0.113.1 dbname=test", "dbname=test"])
def test_remote_or_implicit_database_rejected(tmp_path, database):
    output = _output()
    with pytest.raises(ValueError):
        validate(output, database, tmp_path, uuid4(), "a"*64, "stdlib", 0)
    assert not output.exists()


def test_output_exclusive_and_marker_complete(tmp_path):
    marker(tmp_path, "ready", {"value": 1})
    assert json.loads((tmp_path / "ready.json").read_bytes()) == {"value": 1}
    with pytest.raises(FileExistsError):
        marker(tmp_path, "ready", {"value": 2})
    assert json.loads((tmp_path / "ready.json").read_bytes()) == {"value": 1}
    with pytest.raises(ValueError):
        validate(tmp_path / "other", "host=localhost", tmp_path, uuid4(), "a"*64, "stdlib", 0)


def test_independent_sample_oracle_skips_zero_vectors_and_nontraining_items(tmp_path):
    from scripts.verify_r06_http import _sample
    features = tmp_path / "features"
    features.mkdir()
    # Oracle-only minimal input, not a claim that this is an approved bundle.
    (features / "manifest.json").write_text('{"dimension":2}', encoding="utf-8")
    (features / "items.json").write_text(json.dumps([
        dict(item_id="cold", training_item=False), dict(item_id="zero", training_item=True),
        dict(item_id="seed", training_item=True)]), encoding="utf-8")
    (features / "vectors.f32").write_bytes(struct.pack("<6f", 1, 0, 0, 0, 0, 1))
    assert _sample(tmp_path) == "seed"
    (features / "vectors.f32").write_bytes(struct.pack("<6f", 1, 0, 0, 0, 0, 0))
    with pytest.raises(ValueError):
        _sample(tmp_path)


def test_missing_configuration_is_sanitized(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("EVOREC_DATABASE_URL", raising=False)
    assert main([str(_output()), str(tmp_path), str(uuid4()),
                 "--expected-manifest-sha256", "a"*64]) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "code": "database_not_configured"}


def test_direct_run_cannot_claim_admin_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOREC_ADMIN_TOKEN", "SECRET")
    output = _output()
    with pytest.raises(ValueError):
        run(output, "host=localhost", tmp_path, uuid4(), "a"*64, backend="stdlib")
    assert not output.exists()


@pytest.mark.parametrize("wrong_digest", [False, True])
def test_actual_child_tcp_and_owned_cleanup(isolated_database, tmp_path, monkeypatch, wrong_digest):
    root, target, digest = _build(tmp_path)
    output = _output()
    monkeypatch.setenv("EVOREC_ADMIN_TOKEN", "must-not-reach-child-admin-token-32")
    owned = DemoProcess(output, isolated_database, root, UUID(target.name),
                        "0"*64 if wrong_digest else digest, backend="stdlib", startup_timeout=30)
    if wrong_digest:
        with pytest.raises(RuntimeError):
            with owned:
                pytest.fail("wrong approved hash started a server")
        assert not (output / "ready.json").exists()
    else:
        with owned:
            assert owned.ready["item_count"] == 6 and owned.ready["admin_enabled"] is False
            with httpx.Client(base_url=owned.ready["url"], trust_env=False) as client:
                assert client.get("/health/ready").json()["ready"] is True
                capabilities = client.get("/api/v1/system").json()["capabilities"]
                assert capabilities["r06_serving"] and not capabilities["catalog_publication"]
                assert client.get("/app").status_code == 200
                sample = client.post("/api/v1/sessions", json={"profile_id": "sample"})
                assert sample.status_code == 201 and sample.json()["history"] == ["a"]
                state = sample.json()
                response = client.post("/api/v1/recommendations", json=dict(session_id=state["session_id"],
                    expected_history_version=0, strategy="dense", k=3),
                    headers={"X-Session-Token": state["access_token"], "Idempotency-Key": str(uuid4())})
                assert response.status_code == 200, response.text
                assert response.json()["actual_strategy"] == "dense"
                assert response.json()["bundle_id"] == target.name and len(response.json()["items"]) == 3
                from scripts.verify_r06_http import exercise
                accepted = exercise(client, owned.ready, target, k=3)
                assert accepted["detail_favorite_hide_undo_reset"] and accepted["actual_tcp_http"]
    stopped = json.loads((output / "stopped.json").read_bytes())
    assert stopped["owned_schema_removed"] and stopped["cpu_jobs_drained"]
    with psycopg.connect(isolated_database) as c:
        assert c.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (stopped["schema"],)).fetchone() is None
