"""Real row-lock interlocks: concurrent readers, exclusive session mutations."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event
import time
from uuid import uuid4

import pytest

from evorec.bootstrap import build_demo_application
from evorec.domain.errors import HistoryConflict, IdempotencyInProgress
from evorec.domain.models import FeedbackCommand, FeedbackKind, RecommendationCommand, Strategy
from evorec.infrastructure.postgres import PostgresDemoBackend
from evorec.infrastructure.recommendation_execution import RecommendationExecution
from scripts.seed_demo_catalog import main as seed_demo_catalog


@pytest.fixture
def admission(isolated_database, monkeypatch):
    monkeypatch.delenv("EVOREC_BUNDLE_ROOT", raising=False)
    seed_demo_catalog()
    backend = PostgresDemoBackend(isolated_database, r06_enabled=False)
    session = asyncio.run(backend.create_session())
    command = RecommendationCommand(uuid4(), session.snapshot.session_id, session.access_token,
                                    0, Strategy.POPULAR, 2, 2.)
    try:
        yield backend, command
    finally:
        asyncio.run(backend.aclose())
        assert not backend._executions


def admit(backend, command):
    execution = RecommendationExecution(backend, command)
    try:
        return execution.admit()
    finally:
        execution.close()


def test_distinct_keys_can_capture_same_session_before_first_admission_commits(admission, monkeypatch):
    backend, first = admission
    # Different adapters/connections, one actual session, no mock SQL locks.
    other = PostgresDemoBackend(backend.database_url, r06_enabled=False)
    second = replace(first, request_id=uuid4())
    entered, second_entered, release = Event(), Event(), Event()
    original = backend._capture_context

    def capture(connection, request_id, session, control):
        if request_id == first.request_id:
            entered.set()
            assert release.wait(10), "first capture was not released"
        else:
            second_entered.set()
        return original(connection, request_id, session, control)

    monkeypatch.setattr(backend, "_capture_context", capture)
    monkeypatch.setattr(other, "_capture_context", capture)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_task = pool.submit(admit, backend, first)
        second_task = None
        try:
            assert entered.wait(5)
            second_task = pool.submit(admit, other, second)
            assert second_entered.wait(4), "second read admission serialized behind first session reader"
            assert not first_task.done()
            second_context = second_task.result(timeout=5)
        finally:
            release.set()
            first_context = first_task.result(timeout=5)
            if second_task is not None:
                second_task.result(timeout=5)
    assert first_context.session == second_context.session
    assert first_context.catalog == second_context.catalog
    with backend._connect() as connection:
        assert connection.execute("SELECT count(*) AS n FROM recommendation_requests WHERE session_id=%s",
                                  (first.session_id,)).fetchone()["n"] == 2
        assert connection.execute("SELECT count(*) AS n FROM request_items").fetchone()["n"] == 0
    assert not other._executions


def test_same_key_still_has_exclusive_execution_before_admission_commit(admission, monkeypatch):
    backend, command = admission
    other = PostgresDemoBackend(backend.database_url, r06_enabled=False)
    entered, release = Event(), Event()
    original = backend._capture_context

    def capture(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(backend, "_capture_context", capture)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(admit, backend, command)
        try:
            assert entered.wait(5)
            with pytest.raises(IdempotencyInProgress):
                admit(other, command)
        finally:
            release.set()
            task.result(timeout=5)
    with backend._connect() as connection:
        assert connection.execute("SELECT count(*) AS n FROM recommendation_requests WHERE request_id=%s",
                                  (command.request_id,)).fetchone()["n"] == 1
    assert not other._executions


@pytest.mark.parametrize("mutation", ["reset", "feedback"])
def test_session_writer_waits_for_actual_capture_then_changes_next_snapshot(admission, monkeypatch, mutation):
    backend, command = admission
    result = asyncio.run(build_demo_application(backend).recommend.execute(command))
    reader = replace(command, request_id=uuid4())
    writer = PostgresDemoBackend(backend.database_url, r06_enabled=False)
    entered, release, connected = Event(), Event(), Event()
    pids = {}
    original_capture, original_connect = backend._capture_context, writer._connect

    def capture(connection, *args):
        context = original_capture(connection, *args)
        pids["reader"] = connection.info.backend_pid
        entered.set()
        assert release.wait(10)
        return context

    def connect():
        connection = original_connect()
        pids["writer"] = connection.info.backend_pid
        connected.set()
        return connection

    def mutate():
        if mutation == "reset":
            return writer._reset_session(command.session_id, command.session_token)
        return writer._record_feedback(FeedbackCommand(
            uuid4(), command.session_id, command.session_token, command.request_id,
            result.items[0].item_id, FeedbackKind.HIDE_SET, datetime.now(timezone.utc), True))

    monkeypatch.setattr(backend, "_capture_context", capture)
    monkeypatch.setattr(writer, "_connect", connect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        read_task = pool.submit(admit, backend, reader)
        write_task = None
        try:
            assert entered.wait(5)
            write_task = pool.submit(mutate)
            assert connected.wait(5)
            # Actual server lock dependency, not a sleep-and-not-done oracle.
            with backend._connect() as observer:
                observer.autocommit = True
                deadline = time.monotonic() + 4
                while True:
                    blockers = observer.execute("SELECT pg_blocking_pids(%s) AS blockers",
                                                (pids["writer"],)).fetchone()["blockers"]
                    if pids["reader"] in blockers:
                        break
                    assert time.monotonic() < deadline, "session writer did not wait on the actual reader"
                    time.sleep(.01)
            assert not write_task.done()
        finally:
            release.set()
            captured = read_task.result(timeout=5)
            if write_task is not None:
                updated = write_task.result(timeout=5)
    assert captured.session.history_version == 0 and captured.session.epoch == 0
    assert updated.history_version == 1
    with backend._connect() as connection:
        frozen = connection.execute("SELECT history_version, session_epoch FROM recommendation_requests "
                                    "WHERE request_id=%s", (reader.request_id,)).fetchone()
        assert frozen == {"history_version": 0, "session_epoch": 0}
    with pytest.raises(HistoryConflict):
        admit(backend, replace(command, request_id=uuid4()))
    fresh = admit(backend, replace(command, request_id=uuid4(), expected_history_version=1))
    assert fresh.session.history_version == 1
    if mutation == "reset":
        assert fresh.session.epoch == 1
    else:
        assert result.items[0].item_id in fresh.session.hidden_items
        # Catalog eligibility is independent of per-session hiding. Verify
        # actual selection, not an invented mutation of the shared catalog.
        recommended = asyncio.run(build_demo_application(backend).recommend.execute(
            replace(command, request_id=uuid4(), expected_history_version=1)))
        assert result.items[0].item_id not in {item.item_id for item in recommended.items}
        assert recommended.binding.history_version == 1
