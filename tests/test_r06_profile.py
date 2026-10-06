"""Diagnostic observers preserve behavior and never log request arguments."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import profile_r06_online as profiler
from evorec.domain.errors import ManagementError
from evorec.infrastructure.postgres import PostgresDemoBackend


def test_inactive_sync_observer_returns_original_object_without_logging():
    timing = profiler.RequestTimings()
    result = object()
    assert timing.sync("stage", lambda value: value)(result) is result
    assert timing.requests == [] and timing.current is None


@pytest.mark.parametrize("fail", [False, True])
def test_sync_thread_stage_preserves_identity_errors_and_omits_secrets(fail):
    timing = profiler.RequestTimings()
    result, failure = object(), RuntimeError("DO-NOT-LOG-THIS")
    def work(value, *, token):
        assert value is result and token == "PRIVATE-ARGUMENT"
        if fail: raise failure
        return result
    async def post(client, url, **kwargs):
        value = await asyncio.to_thread(timing.sync("worker", work), result, token="PRIVATE-ARGUMENT")
        assert value is result
        return SimpleNamespace(status_code=200)
    async def run():
        if fail:
            with pytest.raises(RuntimeError) as caught:
                await timing.post(post)(None, "/api/v1/recommendations", json={"strategy": "dense"})
            assert caught.value is failure
        else:
            assert (await timing.post(post)(None, "/api/v1/recommendations", json={"strategy": "dense"})).status_code == 200
    asyncio.run(run())
    assert timing.current is None
    request, = timing.requests
    event, = request["stages"]
    assert event["stage"] == "worker" and event["thread_cpu_seconds"] >= 0 and event["wall_seconds"] >= 0
    assert event["error_type"] == ("RuntimeError" if fail else None)
    assert "PRIVATE" not in json.dumps(timing.requests) and "DO-NOT" not in json.dumps(timing.requests)


@pytest.mark.parametrize("fail", [False, True])
def test_async_observer_preserves_response_and_exception_and_resets_trace(fail):
    timing = profiler.RequestTimings()
    response, failure = SimpleNamespace(status_code=504), RuntimeError("PRIVATE-RESPONSE")
    async def stage():
        await asyncio.sleep(0)
        if fail: raise failure
        return response
    async def post(client, url, **kwargs):
        return await timing.async_stage("async_stage", stage)()
    async def run():
        if fail:
            with pytest.raises(RuntimeError) as caught:
                await timing.post(post)(None, "/api/v1/recommendations", json={"strategy": ["PRIVATE-BODY"]})
            assert caught.value is failure
        else:
            assert await timing.post(post)(None, "/api/v1/recommendations", json={"strategy": "popular"}) is response
    asyncio.run(run())
    request, = timing.requests
    assert timing.current is None and len(request["stages"]) == 1
    assert request["stages"][0]["thread_cpu_seconds"] is None
    assert "PRIVATE" not in json.dumps(timing.requests)


def test_non_recommendation_posts_are_not_observed():
    timing = profiler.RequestTimings()
    response = object()
    async def post(client, url, *, json): return response
    assert asyncio.run(timing.post(post)(None, "/api/v1/sessions", json={"private": "anything"})) is response
    assert timing.requests == [] and timing.current is None


def test_patches_restore_original_methods_after_exception():
    original = PostgresDemoBackend._admit
    timing = profiler.RequestTimings()
    with pytest.raises(RuntimeError):
        with profiler.observe(timing):
            assert PostgresDemoBackend._admit is not original
            raise RuntimeError("injected")
    assert PostgresDemoBackend._admit is original


@pytest.mark.parametrize("service_fails,schemas_restored", [(False, True), (True, True), (False, False)])
def test_diagnostic_control_flow_keeps_service_failure_separate(tmp_path, monkeypatch, service_fails, schemas_restored):
    # Mocked Git/namespace/verifier control-flow test, not model acceptance.
    monkeypatch.setattr(profiler, "__file__", str(tmp_path / "scripts" / "profile_r06_online.py"))
    monkeypatch.setattr(profiler, "_source", lambda _: "a"*40)
    schemas = iter((frozenset(), frozenset() if schemas_restored else frozenset({"rival_schema"})))
    monkeypatch.setattr(profiler, "_schemas", lambda _: next(schemas))
    original = PostgresDemoBackend._admit
    def verify(*args, **kwargs):
        assert PostgresDemoBackend._admit is not original
        assert "scripts/profile_r06_online.py" in profiler.verifier.SOURCE_FILES
        if service_fails: raise ManagementError("injected_failure", "PRIVATE-STORAGE-DETAIL")
    monkeypatch.setattr(profiler.verifier, "verify", verify)
    output = tmp_path / "artifacts" / "profile"
    result = profiler.profile(output, "PRIVATE-CONNECTION", tmp_path, uuid4(), "b"*64)
    assert result["status"] == "diagnostic_completed"
    assert result["service_status"] == ("failed" if service_fails else "passed")
    assert result["temporary_schema_set_restored"] is schemas_restored
    assert result["uninstrumented_acceptance"] is False
    assert PostgresDemoBackend._admit is original
    saved = (output / "profile.json").read_bytes()
    assert json.loads(saved) == result and b"PRIVATE" not in saved
    with pytest.raises(FileExistsError):
        profiler.profile(output, "PRIVATE-CONNECTION", tmp_path, uuid4(), "b"*64)
    assert (output / "profile.json").read_bytes() == saved


def test_unsafe_output_rejected_without_reading_database(tmp_path, monkeypatch):
    monkeypatch.setattr(profiler, "__file__", str(tmp_path / "scripts" / "profile_r06_online.py"))
    monkeypatch.setattr(profiler, "_schemas", lambda _: pytest.fail("must reject before DB access"))
    for output in (tmp_path, tmp_path / "artifacts"):
        with pytest.raises(ValueError): profiler.profile(output, "unused", tmp_path, uuid4(), "b"*64)


def test_failed_service_diagnostic_cli_returns_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE-CONNECTION")
    monkeypatch.setattr(profiler, "profile", lambda *args, **kwargs: dict(
        status="diagnostic_completed", service_status="failed", temporary_schema_set_restored=True))
    assert profiler.main([str(tmp_path), str(tmp_path), str(uuid4()), "--expected-manifest-sha256", "b"*64]) == 1
    assert json.loads(capsys.readouterr().out)["service_status"] == "failed"


def test_sample_profile_option_is_forwarded_and_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(profiler, "__file__", str(tmp_path / "scripts" / "profile_r06_online.py"))
    monkeypatch.setattr(profiler, "_source", lambda _: "a" * 40)
    monkeypatch.setattr(profiler, "_schemas", lambda _: frozenset())
    calls = []
    def verify(*args, **kwargs):
        calls.append(kwargs)
    monkeypatch.setattr(profiler.verifier, "verify", verify)
    result = profiler.profile(tmp_path / "artifacts" / "sample", "PRIVATE-DB", tmp_path,
                              uuid4(), "b" * 64, content_backend="numpy", sample_profile=True)
    assert calls == [{"content_backend": "numpy", "sample_profile": True, "sample_reset": False}]
    assert result["sample_profile"] is True and result["api_deadline_seconds"] == 2.0
    assert result["uninstrumented_acceptance"] is False


@pytest.mark.parametrize("status", [200, 409])
def test_reset_phase_label_requires_success_and_preserves_response(status):
    timing = profiler.RequestTimings()
    response = SimpleNamespace(status_code=status)
    async def post(client, url, **kwargs):
        return response
    async def run():
        observed = timing.post(post)
        assert await observed(None, "/api/v1/sessions/PRIVATE/reset") is response
        assert await observed(None, "/api/v1/recommendations", json={"strategy": "dense"}) is response
    asyncio.run(run())
    request, = timing.requests
    assert request["after_reset"] is (status == 200)
    assert "PRIVATE" not in json.dumps(timing.requests)


def test_gc_callback_only_observes_and_is_removed_on_failure():
    import gc
    timing = profiler.RequestTimings()
    callbacks, enabled, threshold = list(gc.callbacks), gc.isenabled(), gc.get_threshold()
    with pytest.raises(RuntimeError):
        with profiler.observe(timing):
            assert timing.gc in gc.callbacks
            raise RuntimeError("injected")
    assert gc.callbacks == callbacks and gc.isenabled() == enabled and gc.get_threshold() == threshold


def test_gc_stage_records_only_generation_and_elapsed_time():
    timing = profiler.RequestTimings()
    async def post(client, url, **kwargs):
        timing.gc("start", {"generation": 2, "private": "PRIVATE"})
        timing.gc("stop", {"generation": 2, "private": "PRIVATE"})
        return SimpleNamespace(status_code=200)
    asyncio.run(timing.post(post)(None, "/api/v1/recommendations", json={"strategy": "dense"}))
    request, = timing.requests
    event, = request["stages"]
    assert event["stage"] == "gc_generation_2" and event["wall_seconds"] >= 0
    assert "PRIVATE" not in json.dumps(timing.requests)


def test_sample_reset_cli_uses_sample_profile_without_changing_deadline(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EVOREC_DATABASE_URL", "PRIVATE-CONNECTION")
    calls = []
    def profile(*args, **kwargs):
        calls.append(kwargs)
        return dict(service_status="passed", temporary_schema_set_restored=True)
    monkeypatch.setattr(profiler, "profile", profile)
    assert profiler.main([str(tmp_path), str(tmp_path), str(uuid4()), "--expected-manifest-sha256", "b"*64,
                          "--sample-reset"]) == 0
    assert calls == [{"content_backend": "stdlib", "sample_profile": True, "sample_reset": True}]
    assert "PRIVATE" not in capsys.readouterr().out
