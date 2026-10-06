"""Opt-in real PostgreSQL/API wiring, hostile snapshots and actual CPU lifetime."""

import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import json
import struct
from threading import Event
from uuid import UUID, uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb

from evorec.api.app import create_app
from evorec.application.compare import ComparisonCommand
from evorec.bootstrap import build_demo_application
from evorec.domain.errors import IdempotencyReplay, ManagementError, SnapshotMismatch
from evorec.domain.models import FeedbackCommand, FeedbackKind, RecommendationCommand, Strategy
from evorec.infrastructure.comparison_store import decode_result, encode_result
from evorec.infrastructure.postgres import PostgresDemoBackend
from evorec.infrastructure import r06_admission
from evorec.infrastructure.r06_admission import ManagedR06Runtime, restore_request
from evorec.infrastructure.r06_serving import R06SnapshotRanker
from evorec.infrastructure.r06_async import R06CPUQueue
from test_r06_async import _settle, _started
from test_r06_bundle import _build


@pytest.fixture
def online(isolated_database, tmp_path, monkeypatch):
    root, target, digest = _build(tmp_path)
    monkeypatch.setenv("EVOREC_BUNDLE_ROOT", str(root))
    monkeypatch.setenv("EVOREC_R06_SERVING_ENABLED", "1")
    monkeypatch.setenv("EVOREC_R06_CONTENT_BACKEND", "stdlib")
    monkeypatch.setenv("EVOREC_R06_RANKER_BACKEND", "stdlib")
    monkeypatch.setenv("EVOREC_ADMIN_TOKEN", "local-admin-test-token-32-characters")
    application = build_demo_application()
    identity = UUID(target.name)
    application.manager.prepare_r06_bundle(identity, digest)
    application.manager.publish(uuid4(), identity, None)
    try:
        yield application, identity, digest
    finally:
        asyncio.run(application.backend.aclose())


async def _command(application, *, history=(), strategy=Strategy.DENSE, timeout=5):
    created = await application.backend.create_session()
    if history:
        with application.backend._connect() as c:
            c.execute("UPDATE sessions SET history=%s WHERE session_id=%s",
                      (Jsonb(list(history)), created.snapshot.session_id))
    return RecommendationCommand(uuid4(), created.snapshot.session_id, created.access_token,
                                 0, strategy, 6, timeout)


def test_popular_uses_training_heat_not_item_id_or_zero_prior(online):
    app, _, _ = online
    async def run():
        result = await app.recommend.execute(await _command(app, strategy=Strategy.POPULAR))
        # Independent fixture statistics: a=4, zero=3, b=2, c=d=1, e=0.
        assert [(i.item_id, i.score) for i in result.items] == [
            ("a", 4.), ("zero", 3.), ("b", 2.), ("c", 1.), ("d", 1.)]
        assert all(i.source == "r06-training-recent-popular-v1" for i in result.items)
        assert result.actual_strategy == Strategy.POPULAR and result.fallback_reason is None
        assert result.model_version == app.backend.runtime.bundle.model_version
    asyncio.run(run())


def test_full_catalog_capture_restore_reuse_seal_only_after_actual_guards(online, monkeypatch):
    app, _, _ = online
    monkeypatch.setattr(r06_admission, "_seal", lambda *args: pytest.fail("full catalog rehashed"))
    async def run():
        command = await _command(app)
        context = await app.backend.snapshot_for_comparison(command)
        assert context.model.catalog_sha256 == app.backend.runtime.bundle.full_catalog_seal
        assert restore_request(app.backend.runtime.bundle, context).context is context
        # Same cardinality is insufficient: a forged new ID must fail first.
        eligible = (context.catalog.eligible_items - {"a"}) | {"new"}
        hostile = replace(context, catalog=replace(context.catalog, eligible_items=eligible))
        with pytest.raises(ManagementError) as error:
            restore_request(app.backend.runtime.bundle, hostile)
        assert error.value.code == "r06_snapshot_changed"
    asyncio.run(run())


@pytest.mark.parametrize("inactive", [False, True])
def test_capture_uses_compact_actual_rows_without_dropping_inactive_members(online, monkeypatch, inactive):
    import sys
    from evorec.infrastructure import postgres
    app, _, _ = online
    if inactive:
        app.manager.deactivate_item("a")
    original = postgres.capture_model
    observed = []

    def capture(connection, runtime, session, catalog, rows):
        fields = ("item_id", "internal_item_id", "is_active", "r06_model_text", "r06_first_seen_ms")
        # Container budget only, not a process RSS or wall-time assertion. A
        # future equivalent compact representation can satisfy the same test.
        assert len(rows) == 6
        assert all(sys.getsizeof(row) < sys.getsizeof(dict.fromkeys(fields)) for row in rows)
        first = next(row for row in rows if row.item_id == "a")
        assert first.r06_model_text == "中文 alpha" and first.r06_first_seen_ms == 1
        assert first.is_active is (not inactive)
        observed.append(True)
        return original(connection, runtime, session, catalog, rows)

    monkeypatch.setattr(postgres, "capture_model", capture)
    async def run():
        context = await app.backend.snapshot_for_comparison(await _command(app))
        assert len(context.catalog.eligible_items) == (5 if inactive else 6)
        assert restore_request(app.backend.runtime.bundle, context).context is context
    asyncio.run(run())
    assert observed == [True]


@pytest.mark.parametrize("replacement", [frozenset(), frozenset({"b", "c", "d", "e", "zero"}),
                                        frozenset({"b", "c", "d", "e", "zero", "new"}),
                                        frozenset({"a", "b", "c", "d", "e", "zero", "new"})])
def test_capture_rejects_actual_eligibility_coverage_mismatch(online, monkeypatch, replacement):
    from evorec.infrastructure import postgres
    app, _, _ = online
    original = postgres.capture_model

    def capture(connection, runtime, session, catalog, rows):
        return original(connection, runtime, session,
                        replace(catalog, eligible_items=replacement), rows)

    monkeypatch.setattr(postgres, "capture_model", capture)
    async def run():
        with pytest.raises(ManagementError) as error:
            await app.backend.snapshot_for_comparison(await _command(app))
        assert error.value.code == "r06_catalog_changed"
    asyncio.run(run())


@pytest.mark.parametrize("active", [frozenset(), frozenset({"b", "c"})])
def test_subset_capture_restore_compute_exact_seal_and_reject_full_seal(online, active):
    import hashlib
    app, _, _ = online
    bundle = app.backend.runtime.bundle
    with app.backend._connect() as c:
        c.execute("UPDATE items SET is_active=(item_id=ANY(%s))", (list(active),))
    async def run():
        command = await _command(app)
        context = await app.backend.snapshot_for_comparison(command)
        expected = hashlib.sha256(json.dumps(["r06-frozen-bundle-v1", bundle.manifest_sha256,
            sorted((item, bundle.catalog_item_sha256[item]) for item in active)],
            ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        assert context.catalog.eligible_items == active
        assert context.model.catalog_sha256 == expected != bundle.full_catalog_seal
        assert restore_request(bundle, context).context is context
        forged = replace(context, model=replace(context.model, catalog_sha256=bundle.full_catalog_seal))
        with pytest.raises(ManagementError) as error:
            restore_request(bundle, forged)
        assert error.value.code == "r06_snapshot_changed"
    asyncio.run(run())


def test_full_catalog_cache_does_not_approve_deleted_ordered_member(online):
    app, identity, _ = online
    with app.backend._connect() as c:
        c.execute("DELETE FROM bundle_items WHERE bundle_id=%s AND item_id='a'", (identity,))
    async def run():
        command = await _command(app)
        with pytest.raises(ManagementError) as error:
            await app.recommend.execute(command)
        assert error.value.code == "bundle_members_changed"
        with app.backend._connect() as c:
            assert c.execute("SELECT count(*) AS n FROM recommendation_requests").fetchone()["n"] == 0
    asyncio.run(run())


def test_popular_filters_history_and_deactivation_before_top_k(online):
    app, _, _ = online
    app.manager.deactivate_item("zero")
    async def run():
        command = replace(await _command(app, history=("a",), strategy=Strategy.POPULAR), k=2)
        result = await app.recommend.execute(command)
        assert [(i.item_id, i.score) for i in result.items] == [("b", 2.), ("c", 1.)]
    asyncio.run(run())


def test_popular_legacy_saved_results_replay_unchanged_after_fix(online):
    app, _, _ = online
    async def run():
        command = replace(await _command(app, strategy=Strategy.POPULAR), k=3)
        body = dict(session_id=str(command.session_id), expected_history_version=0, strategy="popular", k=3)
        headers = {"X-Session-Token": command.session_token, "Idempotency-Key": str(command.request_id)}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=app)),
                                     base_url="http://test") as client:
            first = await client.post("/api/v1/recommendations", json=body, headers=headers)
            assert first.status_code == 200
            assert [(i["item_id"], i["score"]) for i in first.json()["items"]] == [("a", 4.), ("zero", 3.), ("b", 2.)]
            # Explicit synthetic historical record, not a claim of running old code.
            legacy = [dict(item_id=i, score=1./p, source="r06-frozen-popular-baseline")
                      for p, i in enumerate(("a", "b", "c"), 1)]
            with app.backend._connect() as c:
                c.execute("DELETE FROM request_items WHERE request_id=%s", (command.request_id,))
                c.cursor().executemany(
                    "INSERT INTO request_items (request_id,item_id,position,score,source) VALUES (%s,%s,%s,%s,%s)",
                    [(command.request_id, i["item_id"], p, i["score"], i["source"]) for p, i in enumerate(legacy, 1)])
            replay = await client.post("/api/v1/recommendations", json=body, headers=headers)
            assert replay.status_code == 200
            assert replay.json() == {**first.json(), "items": legacy}
    asyncio.run(run())


@pytest.mark.parametrize("requested", ["generative", "hybrid"])
def test_unloaded_r06_strategy_reports_actual_training_popular_without_fake_generation(online, requested):
    app, _, _ = online
    async def run():
        result = await app.recommend.execute(await _command(app, strategy=Strategy(requested)))
        assert result.requested_strategy == requested and result.actual_strategy == Strategy.POPULAR
        assert result.fallback_reason == "strategy_not_loaded_in_r06"
        assert [(i.item_id, i.score) for i in result.items] == [("a", 4.), ("zero", 3.), ("b", 2.), ("c", 1.), ("d", 1.)]
    asyncio.run(run())


def test_api_dense_independent_scores_adaptive_replay_restart_and_client_rejection(online):
    app, identity, _ = online
    app.manager.deactivate_item("a")
    app.manager.deactivate_item("b")
    async def run():
        command = await _command(app, history=("a",))
        body = dict(session_id=str(command.session_id), expected_history_version=0, strategy="dense", k=6)
        headers = {"X-Session-Token": command.session_token, "Idempotency-Key": str(command.request_id)}
        transport = httpx.ASGITransport(app=create_app(demo_application=app))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for extra in ("model_snapshot", "full_seen", "timestamp_ms", "catalog_sha256", "model_version"):
                assert (await client.post("/api/v1/recommendations", json={**body, extra: "forged"}, headers=headers)).status_code == 422
            response = await client.post("/api/v1/recommendations", json=body, headers=headers)
            assert response.status_code == 200, response.text
            result = response.json()
            f32 = lambda n: struct.unpack("<f", struct.pack("<f", n))[0]
            cf = {item: f32(61/(60+i)) for i,item in enumerate(("zero", "c", "d"), 1)}
            content = {item: f32(61/(60+i)) for i,item in enumerate(("e", "c", "d", "zero"), 1)}
            expected = {item: f32(4*(cf.get(item, 0.)+content[item])) for item in content}
            assert {item["item_id"]: item["score"] for item in result["items"]} == expected
            assert [item["item_id"] for item in result["items"]] == sorted(expected, key=lambda i: (-expected[i], i))
            assert all(item["source"] == "r06-a-frozen-s17" for item in result["items"])
            assert result["bundle_id"] == str(identity) and result["fallback_reason"] is None
            assert result["model_version"] == app.backend.runtime.bundle.model_version
            assert result["captured_at_ms"] > 10
            assert (await client.post("/api/v1/recommendations", json=body, headers=headers)).json() == result
            assert (await client.post("/api/v1/recommendations", json={**body, "k": 1}, headers=headers)).status_code == 409
            assert (await client.post("/api/v1/recommendations", json=body, headers={**headers, "X-Session-Token": "wrong"})).status_code == 401
            adaptive = await client.post("/api/v1/recommendations", json={**body, "strategy": "adaptive"},
                                         headers={**headers, "Idempotency-Key": str(uuid4())})
            assert adaptive.status_code == 200
            assert adaptive.json()["actual_strategy"] == "dense" and adaptive.json()["items"] == result["items"]
            assert (await client.get("/api/v1/system")).json()["capabilities"]["r06_serving"]
        restarted = build_demo_application()
        try:
            assert (await restarted.readiness.execute()).ready
            with pytest.raises(IdempotencyReplay) as replay:
                await restarted.recommend.execute(command)
            assert replay.value.result.model_version == result["model_version"]
            assert replay.value.result.captured_at_ms == result["captured_at_ms"]
            with app.backend._connect() as c:
                saved = c.execute("SELECT model_snapshot FROM recommendation_requests WHERE request_id=%s",
                                  (command.request_id,)).fetchone()["model_snapshot"]
                assert saved["full_seen"] == ["a"] and saved["model_version"] == result["model_version"]
        finally: await restarted.backend.aclose()
    asyncio.run(run())


def test_full_seen_includes_exposure_removed_state_old_history_and_resets_by_epoch(online):
    app, _, _ = online
    async def run():
        # Only the content path can expose zero-prior e; it is no longer a hot item.
        first = await _command(app, history=("a",), strategy=Strategy.DENSE)
        result = await app.recommend.execute(first)
        for item, kind, desired in (("c", FeedbackKind.EXPOSURE, None),
                                    ("d", FeedbackKind.FAVORITE_SET, True),
                                    ("d", FeedbackKind.FAVORITE_SET, False),
                                    ("e", FeedbackKind.HIDE_SET, True),
                                    ("e", FeedbackKind.HIDE_SET, False)):
            await app.backend.record_feedback(FeedbackCommand(
                uuid4(), first.session_id, first.session_token, first.request_id, item, kind,
                datetime.now(timezone.utc), desired,
                .5 if kind == FeedbackKind.EXPOSURE else None,
                1000 if kind == FeedbackKind.EXPOSURE else None))
        history = ("a",) + ("unknown",)*50
        with app.backend._connect() as c:
            c.execute("UPDATE sessions SET history=%s WHERE session_id=%s", (Jsonb(list(history)), first.session_id))
        session = await app.backend.get_session(first.session_id, first.session_token)
        command = replace(first, request_id=uuid4(), expected_history_version=session.history_version)
        context = await app.backend.snapshot_for_comparison(command)
        assert context.session.history == history
        assert context.model.full_seen == frozenset({"a", "c", "d", "e", "unknown"})
        ranked = await app.backend.rank(context, command)
        assert {item.item_id for item in ranked.candidates} == {"b", "zero"}
        reset = await app.backend.reset_session(first.session_id, first.session_token)
        fresh = await app.backend.snapshot_for_comparison(replace(command, expected_history_version=reset.history_version))
        assert fresh.model.full_seen == frozenset() and fresh.session.epoch == 1
        assert result.model_version == fresh.model.model_version
    asyncio.run(run())


@pytest.mark.parametrize("column,value", [("r06_model_text", "tampered"), ("r06_first_seen_ms", 99)])
def test_actual_sql_content_drift_refuses_admission_without_request_row(online, column, value):
    from psycopg import sql
    app, _, _ = online
    with app.backend._connect() as c:
        c.execute(sql.SQL("UPDATE items SET {}=%s WHERE item_id='a'").format(sql.Identifier(column)), (value,))
    async def run():
        command = await _command(app)
        with pytest.raises(ManagementError) as error: await app.recommend.execute(command)
        assert error.value.code == "r06_catalog_changed"
        with app.backend._connect() as c:
            assert c.execute("SELECT count(*) AS n FROM recommendation_requests").fetchone()["n"] == 0
        restarted = build_demo_application()
        try:
            assert not (await restarted.readiness.execute()).ready
            assert not restarted.manager.publication_state()["admission_open"]
        finally: await restarted.backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["manifest", "version", "seal", "eligible", "bundle", "seen", "time"])
def test_persisted_snapshot_mismatch_refused_before_cpu(online, change):
    app, _, _ = online
    async def run():
        command = await _command(app, history=("a",))
        context = await app.backend.snapshot_for_comparison(command)
        if change == "eligible": context = replace(context, catalog=replace(context.catalog, eligible_items={"a"}))
        elif change == "bundle": context = replace(context, catalog=replace(context.catalog, bundle_id=uuid4()))
        else:
            key, value = {"manifest": ("manifest_sha256", "a"*64), "version": ("model_version", "b"*64),
                          "seal": ("catalog_sha256", "c"*64), "seen": ("full_seen", set()),
                          "time": ("timestamp_ms", 0)}[change]
            context = replace(context, model=replace(context.model, **{key: value}))
        with pytest.raises(ManagementError):
            request = restore_request(app.backend.runtime.bundle, context)
            # Known history must predate capture even if the seal itself is valid.
            app.backend.runtime.bundle.adapter.score(request)
        assert app.backend.r06_queue.outstanding == 0
    asyncio.run(run())


def test_snapshot_serde_immutable_and_legacy_decode(online):
    app, _, _ = online
    async def run():
        command = await _command(app)
        comparison = ComparisonCommand(command.request_id, command.session_id, command.session_token, 0,
                                       (Strategy.POPULAR, Strategy.DENSE), 3)
        snapshot = await app.compare.capture(comparison)
        encoded = encode_result(snapshot)
        assert decode_result(json.loads(json.dumps(encoded))) == snapshot
        with pytest.raises(FrozenInstanceError): snapshot.context.model.timestamp_ms = 0
        encoded["model_snapshot"]["full_seen"].append("other")
        assert snapshot.context.model.full_seen == frozenset()
        encoded.pop("model_snapshot")
        assert decode_result(encoded).context.model is None
    asyncio.run(run())


def test_frozen_job_restart_survives_mutable_catalog_and_session_reset(online):
    app, _, _ = online
    async def run():
        command = await _command(app, history=("a",))
        comparison = ComparisonCommand(command.request_id, command.session_id, command.session_token, 0,
                                       (Strategy.POPULAR, Strategy.DENSE, Strategy.ADAPTIVE), 3)
        await app.comparison_jobs.enqueue(comparison)
        with app.backend._connect() as c:
            frozen = c.execute("SELECT frozen_input FROM strategy_comparison_jobs WHERE job_id=%s",
                               (comparison.comparison_id,)).fetchone()["frozen_input"]
            c.execute("UPDATE items SET is_active=false, r06_model_text='changed' WHERE item_id='c'")
        await app.backend.reset_session(command.session_id, command.session_token)
        restarted = build_demo_application()
        try:
            assert await restarted.comparison_jobs.run_next()
            result = await restarted.compare.get(command.request_id, command.session_id, command.session_token)
            assert result.context == decode_result(frozen).context
            assert result.context.model.full_seen == {"a"} and result.context.session.epoch == 0
            assert result.strategies[1].items == result.strategies[2].items
            assert all(item.source == "r06-a-frozen-s17" for item in result.strategies[1].items)
            assert await restarted.compare.save(comparison) == result
            assert not await restarted.comparison_jobs.run_next()
        finally: await restarted.backend.aclose()
    asyncio.run(run())


def test_disabled_restart_closes_barrier_but_completed_recommendation_replays(online, monkeypatch):
    app, _, _ = online
    async def run():
        command = await _command(app)
        result = await app.recommend.execute(command)
        monkeypatch.setenv("EVOREC_R06_SERVING_ENABLED", "0")
        restarted = build_demo_application()
        try:
            assert not (await restarted.readiness.execute()).ready
            assert not restarted.manager.publication_state()["admission_open"]
            with pytest.raises(IdempotencyReplay) as replay: await restarted.recommend.execute(command)
            assert replay.value.result == result
            with pytest.raises(RuntimeError): await restarted.recommend.execute(replace(command, request_id=uuid4()))
        finally: await restarted.backend.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("flag,backend", [("true", "stdlib"), ("0", "auto"), ("1", "torch")])
def test_explicit_configuration_rejects_typos_and_auto_fallback(monkeypatch, flag, backend):
    monkeypatch.setenv("EVOREC_R06_SERVING_ENABLED", flag)
    monkeypatch.setenv("EVOREC_R06_CONTENT_BACKEND", backend)
    with pytest.raises(ValueError): PostgresDemoBackend("unused")


@pytest.mark.parametrize("history", [("unknown",)*10001, tuple(f"unknown{i}" for i in range(10001))])
def test_input_caps_reject_not_truncate_and_leave_no_accepted_row(online, history):
    app, _, _ = online
    async def run():
        command = await _command(app, history=history)
        with pytest.raises(ManagementError): await app.recommend.execute(command)
        with app.backend._connect() as c:
            assert c.execute("SELECT count(*) AS n FROM recommendation_requests").fetchone()["n"] == 0
    asyncio.run(run())


def test_wrong_model_batch_and_result_never_persist(online, monkeypatch):
    app, _, _ = online
    original = app.backend.rank
    async def wrong(context, command): return replace(await original(context, command), model_version="a"*64)
    monkeypatch.setattr(app.backend, "rank", wrong)
    async def run():
        command = await _command(app)
        with pytest.raises(SnapshotMismatch): await app.recommend.execute(command)
        comparison = ComparisonCommand(uuid4(), command.session_id, command.session_token, 0,
                                       (Strategy.POPULAR, Strategy.DENSE), 3)
        with pytest.raises(SnapshotMismatch): await app.compare.save(comparison)
        with app.backend._connect() as c:
            assert c.execute("SELECT status FROM recommendation_requests WHERE request_id=%s",
                             (command.request_id,)).fetchone()["status"] == "failed"
            assert c.execute("SELECT count(*) AS n FROM request_items").fetchone()["n"] == 0
            assert c.execute("SELECT count(*) AS n FROM strategy_comparisons").fetchone()["n"] == 0
        monkeypatch.setattr(app.backend, "rank", original)
        result = await app.recommend.execute(replace(command, request_id=uuid4()))
        with pytest.raises(SnapshotMismatch): await app.backend.save(replace(result, model_version="a"*64))
    asyncio.run(run())


def test_repeated_cancellation_drains_actual_cpu_before_failure_write(online, monkeypatch):
    app, _, _ = online
    entered, release, exited = Event(), Event(), Event()
    original = R06SnapshotRanker.score
    def block(self, request):
        entered.set()
        assert release.wait(5)
        exited.set()
        return original(self, request)
    monkeypatch.setattr(R06SnapshotRanker, "score", block)
    async def run():
        command = await _command(app)
        task = asyncio.create_task(app.recommend.execute(command))
        try:
            await _started(entered)
            for _ in range(3):
                task.cancel(); await asyncio.sleep(.01)
                assert not task.done() and not exited.is_set()
                with app.backend._connect() as c:
                    assert c.execute("SELECT status FROM recommendation_requests WHERE request_id=%s",
                                     (command.request_id,)).fetchone()["status"] == "accepted"
            release.set()
            with pytest.raises(asyncio.CancelledError): await task
            assert exited.is_set() and app.backend.r06_queue.outstanding == 0
            with app.backend._connect() as c:
                assert c.execute("SELECT status FROM recommendation_requests WHERE request_id=%s",
                                 (command.request_id,)).fetchone()["status"] == "failed"
                assert c.execute("SELECT count(*) AS n FROM request_items").fetchone()["n"] == 0
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_cancelled_admission_thread_does_not_leave_accepted_orphan(online, monkeypatch):
    app, _, _ = online
    entered, release = Event(), Event()
    original = app.backend._admit
    def block(command):
        entered.set(); assert release.wait(5)
        return original(command)
    monkeypatch.setattr(app.backend, "_admit", block)
    async def run():
        command = await _command(app)
        task = asyncio.create_task(app.recommend.execute(command))
        try:
            await _started(entered)
            for _ in range(3):
                task.cancel(); await asyncio.sleep(.01); assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError): await task
            with app.backend._connect() as c:
                assert c.execute("SELECT status FROM recommendation_requests WHERE request_id=%s",
                                 (command.request_id,)).fetchone()["status"] == "failed"
            assert app.backend.r06_queue.outstanding == 0
        finally: release.set(); await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_inflight_old_model_and_idempotency_survive_new_publication_and_rollback(online, tmp_path, monkeypatch):
    import shutil
    app, identity, _ = online
    next_root = tmp_path / "next-inputs"
    next_root.mkdir()
    _, target, digest = _build(next_root)
    new_id = UUID(target.name)
    shutil.copytree(target, app.manager.managed_root / target.name)
    app.manager.prepare_r06_bundle(new_id, digest)
    old_model = app.backend.runtime.bundle.model_version
    entered, release = Event(), Event()
    original = R06SnapshotRanker.score
    def block(self, request):
        entered.set(); assert release.wait(5)
        return original(self, request)
    monkeypatch.setattr(R06SnapshotRanker, "score", block)
    async def run():
        command = await _command(app, history=("a",))
        task = asyncio.create_task(app.recommend.execute(command))
        try:
            await _started(entered)
            await asyncio.to_thread(app.manager.publish, uuid4(), new_id, identity)
            assert app.backend.runtime.bundle.model_version != old_model
            release.set()
            result = await task
            assert result.binding.bundle_id == identity and result.model_version == old_model
            with pytest.raises(IdempotencyReplay) as replay: await app.recommend.execute(command)
            assert replay.value.result == result
            next_result = await app.recommend.execute(replace(command, request_id=uuid4()))
            assert next_result.binding.bundle_id == new_id and next_result.model_version != old_model
            await asyncio.to_thread(app.manager.rollback, uuid4(), identity, new_id)
            assert app.backend.runtime.bundle.model_version == old_model
        finally: release.set(); await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_background_repeat_cancel_retains_lock_until_cpu_finishes(online, monkeypatch):
    app, _, _ = online
    entered, release = Event(), Event()
    original = R06SnapshotRanker.score
    def block(self, request):
        entered.set(); assert release.wait(5)
        return original(self, request)
    monkeypatch.setattr(R06SnapshotRanker, "score", block)
    async def run():
        command = await _command(app)
        comparison = ComparisonCommand(command.request_id, command.session_id, command.session_token, 0,
                                       (Strategy.DENSE, Strategy.POPULAR), 3)
        await app.comparison_jobs.enqueue(comparison)
        task = asyncio.create_task(app.comparison_jobs.run_next())
        try:
            await _started(entered)
            for _ in range(3):
                task.cancel(); await asyncio.sleep(.01)
                assert not task.done()
                assert not await app.comparison_jobs.run_next()  # Cannot steal locked running job.
            await app.comparison_jobs.cancel(command.request_id, command.session_id, command.session_token)
            release.set()
            with pytest.raises(asyncio.CancelledError): await task
            assert app.backend.r06_queue.outstanding == 0
            with app.backend._connect() as c:
                assert c.execute("SELECT count(*) AS n FROM strategy_comparisons").fetchone()["n"] == 0
        finally: release.set(); await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_registered_hash_change_fails_job_without_popular_fallback(online):
    app, identity, _ = online
    async def run():
        command = await _command(app)
        comparison = ComparisonCommand(command.request_id, command.session_id, command.session_token, 0,
                                       (Strategy.DENSE, Strategy.POPULAR), 3)
        await app.comparison_jobs.enqueue(comparison)
        with app.backend._connect() as c:
            c.execute("UPDATE bundle_versions SET manifest_sha256=%s WHERE bundle_id=%s", ("a"*64, identity))
        assert await app.comparison_jobs.run_next()
        with app.backend._connect() as c:
            job = c.execute("SELECT status, error_code FROM strategy_comparison_jobs WHERE job_id=%s",
                            (command.request_id,)).fetchone()
            assert job == dict(status="failed", error_code="component_changed")
            assert c.execute("SELECT count(*) AS n FROM strategy_comparisons").fetchone()["n"] == 0
    asyncio.run(run())


def test_lifespan_closes_cpu_queue_after_pending_work(online):
    app, _, _ = online
    api = create_app(demo_application=app)
    async def run():
        async with api.router.lifespan_context(api):
            assert await app.backend.r06_queue.run(lambda: 9) == 9
        with pytest.raises(ManagementError) as error: await app.backend.r06_queue.run(lambda: 9)
        assert error.value.code == "r06_queue_closed"
    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "publish", "report", "popular"])
def test_real_verifier_owns_schema_and_never_publishes_partial_report(online, tmp_path, monkeypatch, failure):
    from pathlib import Path
    import psycopg
    from scripts import verify_r06_online as verifier
    app, identity, digest = online
    # Control-flow test only: real audit execution uses non-mocked Git/source copies.
    monkeypatch.setattr(verifier, "__file__", str(tmp_path / "scripts" / "verify_r06_online.py"))
    monkeypatch.setattr(verifier, "SOURCE_FILES", ())
    monkeypatch.setattr(verifier, "_source", lambda _: "a"*40)
    output = tmp_path / "artifacts" / "verification"
    report = output / "verification.json"
    if failure == "publish":
        def fail(*args): raise ManagementError("injected_failure", "test failure")
        monkeypatch.setattr(type(app.manager), "publish", fail)
    elif failure == "popular":
        from evorec.infrastructure.r06_retrieval import R06Retrieval
        monkeypatch.setattr(R06Retrieval, "popular", lambda *args, **kwargs: (("a", 1.), ("b", .5)))
    elif failure == "report":
        original = Path.open
        def fail(path, *args, **kwargs):
            if path == report: raise OSError("injected report failure")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "open", fail)
    with psycopg.connect(app.backend.database_url) as c:
        before = c.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'test_evorec_%'").fetchall()
    if failure:
        with pytest.raises((ManagementError, OSError, ValueError)):
            verifier.verify(output, app.backend.database_url, app.manager.managed_root, identity, digest)
        assert not report.exists()
    else:
        result = verifier.verify(output, app.backend.database_url, app.manager.managed_root, identity, digest)
        assert result["status"] == "passed" and result["owned_temporary_schema_removed"]
        saved = report.read_bytes()
        with pytest.raises(FileExistsError):
            verifier.verify(output, app.backend.database_url, app.manager.managed_root, identity, digest)
        assert report.read_bytes() == saved
    with psycopg.connect(app.backend.database_url) as c:
        assert c.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'test_evorec_%'").fetchall() == before


@pytest.mark.parametrize("mutation", ["missing", "internal_gap"])
def test_loaded_runtime_does_not_treat_membership_drift_as_deactivation(online, mutation):
    app, identity, _ = online
    with app.backend._connect() as c:
        if mutation == "missing":
            c.execute("DELETE FROM bundle_items WHERE bundle_id=%s AND item_id='a'", (identity,))
        else:
            c.execute("UPDATE bundle_items SET internal_item_id=99 WHERE bundle_id=%s AND item_id='zero'", (identity,))
    async def run():
        with pytest.raises(ManagementError) as error: await app.recommend.execute(await _command(app))
        assert error.value.code == "bundle_members_changed"
        with app.backend._connect() as c:
            assert c.execute("SELECT count(*) AS n FROM recommendation_requests").fetchone()["n"] == 0
    asyncio.run(run())


@pytest.mark.parametrize("sample_reset", [False, True])
def test_sample_profile_verifier_uses_actual_seed_not_validation_reference(online, tmp_path, monkeypatch, sample_reset):
    from scripts import verify_r06_online as verifier
    app, identity, digest = online
    monkeypatch.setattr(verifier, "__file__", str(tmp_path / "scripts" / "verify_r06_online.py"))
    monkeypatch.setattr(verifier, "SOURCE_FILES", ())
    monkeypatch.setattr(verifier, "_source", lambda _: "a" * 40)
    monkeypatch.setattr(verifier, "_validation", lambda *args: pytest.fail("sample used validation history"))
    result = verifier.verify(tmp_path / "artifacts" / "sample", app.backend.database_url,
                             app.manager.managed_root, identity, digest, sample_profile=True, sample_reset=sample_reset)
    assert result["sample_profile"] is True and result["history_count"] == 1
    assert result["status"] == "passed" and result["owned_temporary_schema_removed"]
    assert result["sample_reset"] is sample_reset


def test_sample_reset_verifier_stops_on_third_new_timeout_without_pass_report(online, tmp_path, monkeypatch):
    from scripts import verify_r06_online as verifier
    app, identity, digest = online
    monkeypatch.setattr(verifier, "__file__", str(tmp_path / "scripts" / "verify_r06_online.py"))
    monkeypatch.setattr(verifier, "SOURCE_FILES", ())
    monkeypatch.setattr(verifier, "_source", lambda _: "a" * 40)
    original = httpx.AsyncClient.post
    after_reset, count = False, 0
    async def post(client, path, **kwargs):
        nonlocal after_reset, count
        if after_reset and path == "/api/v1/recommendations":
            # Only fresh keys; completed replays must not advance the count.
            if kwargs["headers"]["Idempotency-Key"] not in keys:
                keys.add(kwargs["headers"]["Idempotency-Key"])
                count += 1
                if count == 3:
                    return httpx.Response(504, json={"error": {"code": "recommendation_timeout"}})
        response = await original(client, path, **kwargs)
        if path.endswith("/reset") and response.status_code == 200:
            after_reset = True
        return response
    keys = set()
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    output = tmp_path / "artifacts" / "reset"
    with pytest.raises(ManagementError) as error:
        verifier.verify(output, app.backend.database_url, app.manager.managed_root, identity, digest,
                        sample_profile=True, sample_reset=True)
    assert error.value.code == "recommendation_timeout" and count == 3
    assert not (output / "verification.json").exists()


def test_cached_original_sources_are_immutable_owned_and_hash_bound(online):
    app, _, _ = online
    runtime = app.backend.runtime
    records = dict(runtime.catalog_items)
    copied = ManagedR06Runtime(runtime.bundle, records)
    records.clear()
    assert len(copied.catalog_items) == 6
    with pytest.raises(TypeError): copied.catalog_items["a"] = None
    with pytest.raises(FrozenInstanceError): copied.catalog_items["a"].text = "tampered"
    changed = dict(runtime.catalog_items)
    changed["a"] = replace(changed["a"], text="tampered")
    with pytest.raises(ManagementError): ManagedR06Runtime(runtime.bundle, changed)


def test_api_capacity_error_is_explicit_and_never_returns_popular(online):
    app, _, _ = online
    app.backend.r06_queue.close()
    app.backend.r06_queue = R06CPUQueue(workers=1, queued=0)
    entered, release = Event(), Event()
    def block(): entered.set(); assert release.wait(5)
    async def run():
        running = asyncio.create_task(app.backend.r06_queue.run(block))
        try:
            await _started(entered)
            command = await _command(app)
            body = dict(session_id=str(command.session_id), expected_history_version=0, strategy="dense", k=3)
            headers = {"X-Session-Token": command.session_token, "Idempotency-Key": str(command.request_id)}
            api = create_app(demo_application=app)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
                response = await client.post("/api/v1/recommendations", json=body, headers=headers)
                assert response.status_code == 429
                assert response.json()["error"]["code"] == "r06_queue_full" and response.json()["error"]["retryable"]
                assert "items" not in response.json()
            assert "429" in api.openapi()["paths"]["/api/v1/recommendations"]["post"]["responses"]
            with app.backend._connect() as c:
                assert c.execute("SELECT status FROM recommendation_requests WHERE request_id=%s",
                                 (command.request_id,)).fetchone()["status"] == "failed"
                assert c.execute("SELECT count(*) AS n FROM request_items").fetchone()["n"] == 0
        finally: release.set(); await running
    asyncio.run(run())
