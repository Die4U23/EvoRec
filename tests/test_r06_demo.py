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
    # pytest --basetemp may itself be under project/artifacts. Derive an
    # explicitly forbidden path instead of assuming tmp_path is outside it.
    forbidden = Path(__file__).resolve().parents[1] / "tmp" / uuid4().hex
    with pytest.raises(ValueError, match="fresh artifacts subdirectory"):
        validate(forbidden, "host=localhost", tmp_path, uuid4(), "a"*64, "stdlib", 0)
    assert not forbidden.exists()


def test_output_validation_accepts_fresh_artifacts_and_rejects_existing(tmp_path):
    output = _output()
    validated, _, _ = validate(output, "host=localhost", tmp_path, uuid4(), "a"*64, "stdlib", 0)
    assert validated == output and not output.exists()
    output.mkdir(parents=True)
    with pytest.raises(FileExistsError, match="already exists"):
        validate(output, "host=localhost", tmp_path, uuid4(), "a"*64, "stdlib", 0)
    assert not (output / "ready.json").exists()


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
                assert accepted["post_reset_fresh_dense_trials"] == 3
                assert accepted["post_reset_dense_matches_initial_ids"]
                assert accepted["post_reset_dense_replay_exact"]
    stopped = json.loads((output / "stopped.json").read_bytes())
    assert stopped["owned_schema_removed"] and stopped["cpu_jobs_drained"]
    with psycopg.connect(isolated_database) as c:
        assert c.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (stopped["schema"],)).fetchone() is None


@pytest.mark.parametrize("fault,trial", [
    ("timeout", 1), ("timeout", 3), ("identity", 1), ("binding", 1), ("items", 1), ("replay", 1),
])
def test_tcp_verifier_rejects_fresh_post_reset_failure(isolated_database, tmp_path, fault, trial):
    from evorec.domain.errors import ManagementError
    from scripts.verify_r06_http import exercise

    root, target, digest = _build(tmp_path)
    owned = DemoProcess(_output(), isolated_database, root, UUID(target.name), digest,
                        backend="stdlib", startup_timeout=30)
    with owned, httpx.Client(base_url=owned.ready["url"], timeout=10, trust_env=False) as client:
        class FaultClient:
            # All setup, feedback and reset use real TCP. Only the named response
            # is altered: this tests verifier rejection, not model performance.
            reset_version = None
            fresh_keys = []
            triggered = False

            def get(self, *args, **kwargs):
                return client.get(*args, **kwargs)

            def post(self, path, **kwargs):
                response = client.post(path, **kwargs)
                if path.endswith("/reset"):
                    self.reset_version = response.json()["history_version"]
                body = kwargs.get("json", {})
                if (path != "/api/v1/recommendations" or self.reset_version is None
                        or body.get("strategy") != "dense"
                        or body.get("expected_history_version") != self.reset_version):
                    return response
                key = kwargs["headers"]["Idempotency-Key"]
                replay = key in self.fresh_keys
                if not replay:
                    self.fresh_keys.append(key)
                if len(self.fresh_keys) != trial or replay != (fault == "replay"):
                    return response
                self.triggered = True
                assert response.status_code == 200, response.text
                value = response.json()
                if fault == "timeout":
                    return httpx.Response(504, json={"error": {"code": "recommendation_timeout"}})
                if fault == "identity":
                    value["model_version"] = "0" * 64
                elif fault == "binding":
                    value["session_id"] = str(uuid4())
                elif fault == "items":
                    # Still k unique catalog IDs, but one original item is
                    # replaced. A count/seed-only check is insufficient.
                    present = {item["item_id"] for item in value["items"]}
                    value["items"][0]["item_id"] = next(
                        item for item in ("b", "c", "d", "e", "zero") if item not in present)
                else:
                    value["items"][0]["score"] += 1
                return httpx.Response(200, json=value)

        altered = FaultClient()
        with pytest.raises((ManagementError, ValueError)):
            exercise(altered, owned.ready, target, k=3)
        assert altered.triggered
        assert len(altered.fresh_keys) == trial  # No automatic retry of a failure.
    assert owned.stopped["cpu_jobs_drained"] and owned.stopped["owned_schema_removed"]


def test_tcp_observer_preserves_response_and_omits_all_private_inputs():
    from scripts.verify_r06_http import ObservedClient
    response = httpx.Response(200, json={"private": "DO-NOT-LOG-RESPONSE"})

    class Client:
        def post(self, path, **kwargs):
            return response
        def get(self, path):
            return response

    observed = ObservedClient(Client())
    assert observed.get("/api/v1/system") is response
    for key in ("PRIVATE-KEY", "PRIVATE-KEY"):
        assert observed.post("/api/v1/recommendations", json={"strategy": "dense", "session_id": "PRIVATE-ID"},
                             headers={"Idempotency-Key": key, "X-Session-Token": "PRIVATE-TOKEN"}) is response
    assert observed.post("/api/v1/sessions/PRIVATE-ID/reset") is response
    observed.post("/api/v1/recommendations", json={"strategy": ["PRIVATE-BODY"]},
                  headers={"Idempotency-Key": "PRIVATE-NEW-KEY"})
    assert [r["repeated_key"] for r in observed.records] == [False, True, False]
    assert [r["after_reset"] for r in observed.records] == [False, False, True]
    assert observed.records[-1]["strategy"] == "other"
    assert all(r["status_code"] == 200 and r["wall_seconds"] >= 0 for r in observed.records)
    assert "PRIVATE" not in json.dumps(observed.records) and "DO-NOT" not in json.dumps(observed.records)


def test_tcp_observer_keeps_timeout_exception_and_failed_reset_separate():
    from scripts.verify_r06_http import ObservedClient
    failure = httpx.ReadTimeout("PRIVATE-ERROR")

    class Client:
        def post(self, path, **kwargs):
            if path.endswith("/reset"):
                return httpx.Response(409)
            raise failure

    observed = ObservedClient(Client())
    observed.post("/api/v1/sessions/PRIVATE-ID/reset")
    with pytest.raises(httpx.ReadTimeout) as caught:
        observed.post("/api/v1/recommendations", json={"strategy": "dense"},
                      headers={"Idempotency-Key": "PRIVATE-KEY"})
    assert caught.value is failure
    record, = observed.records
    assert record["status_code"] is None and record["after_reset"] is False
    assert "PRIVATE" not in json.dumps(observed.records)


@pytest.mark.parametrize("failure", ["exercise", "cleanup", None])
def test_tcp_verifier_keeps_partial_observations_without_false_pass(tmp_path, monkeypatch, failure):
    from scripts import verify_r06_http as verifier
    from evorec.domain.errors import ManagementError

    # Mocked verifier lifecycle, not a real model/network/cleanup acceptance.
    monkeypatch.setattr(verifier, "__file__", str(tmp_path / "scripts" / "verify_r06_http.py"))
    monkeypatch.setattr(verifier, "_source", lambda _: "a" * 40)
    monkeypatch.setattr(verifier, "_schemas", lambda _: frozenset())
    monkeypatch.setattr(verifier, "SOURCE_FILES", ())

    class Process:
        ready = {"url": "http://test", "item_count": 6, "bundle_id": str(uuid4()),
                 "model_version": "b" * 64, "manifest_sha256": "c" * 64}
        stopped = None
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args):
            if failure == "cleanup": raise RuntimeError("PRIVATE-CLEANUP-ERROR")
            self.stopped = {"cpu_jobs_drained": True, "owned_schema_removed": True}

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs): return httpx.Response(504 if failure == "exercise" else 200)

    def exercise(client, *args):
        response = client.post("/api/v1/recommendations", json={"strategy": "dense"})
        if response.status_code != 200:
            raise ManagementError("recommendation_timeout", "PRIVATE-ERROR", 504)
        return {}

    monkeypatch.setattr(verifier, "DemoProcess", Process)
    monkeypatch.setattr(verifier.httpx, "Client", Client)
    monkeypatch.setattr(verifier, "exercise", exercise)
    output = tmp_path / "artifacts" / "tcp"
    if failure:
        with pytest.raises((ManagementError, RuntimeError)):
            verifier.verify(output, "PRIVATE-DB", tmp_path, uuid4(), "c" * 64)
        assert not (output / "verification.json").exists()
    else:
        assert verifier.verify(output, "PRIVATE-DB", tmp_path, uuid4(), "c" * 64)["status"] == "passed"
    saved = (output / "request-observations.json").read_bytes()
    record = json.loads(saved)
    assert record["status"] == "observations_only"
    assert record["requests"][0]["status_code"] == (504 if failure == "exercise" else 200)
    assert record["stopped"] is (failure != "cleanup")
    assert record["cpu_jobs_drained"] is (failure != "cleanup")
    assert b"PRIVATE" not in saved
