"""Controlled demo ranking uses bounded CPU work without blocking its event loop."""

import asyncio
from threading import Event, get_ident
from types import SimpleNamespace
import unittest
from uuid import uuid4

from evorec.domain.errors import ManagementError
from evorec.domain.models import (
    CatalogSnapshot, RecommendationCommand, RequestContext, SessionSnapshot, Strategy,
)
from evorec.infrastructure.postgres import PostgresDemoBackend
from evorec.infrastructure.r06_async import R06CPUQueue


class DemoRankAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setup_backend(self, score):
        backend = PostgresDemoBackend("unused", r06_enabled=False)
        self.addAsyncCleanup(backend.aclose)
        session = SessionSnapshot(uuid4(), 0, 0, ("a", "missing"), frozenset())
        catalog = CatalogSnapshot(uuid4(), 0, frozenset({"a", "b", "c"}))
        context = RequestContext(uuid4(), session, catalog)
        command = RecommendationCommand(context.request_id, session.session_id, "fake-token",
                                        0, Strategy.DENSE, 2, 5.0)
        runtime = SimpleNamespace(bundle_id=str(catalog.bundle_id), ranking_budget=2,
                                  item_ids=("a", "b", "c"),
                                  _item_indices={"a": 0, "b": 1, "c": 2}, score=score)
        backend.runtimes[runtime.bundle_id] = runtime
        return backend, context, command, runtime

    async def wait_started(self, started):
        self.assertTrue(await asyncio.wait_for(asyncio.to_thread(started.wait, 2), 3))

    async def test_event_loop_progresses_while_scoring_is_blocked(self):
        started, release = Event(), Event()
        worker_threads = []
        owner_thread = get_ident()

        def score(history, candidates):
            worker_threads.append(get_ident())
            started.set()
            release.wait(2)
            return (2.0, 1.0)

        backend, context, command, _ = self.setup_backend(score)
        task = asyncio.create_task(backend.rank(context, command))
        try:
            await self.wait_started(started)
            self.assertFalse(task.done(), "CPU work blocked the event loop until scoring finished")
            self.assertNotEqual(worker_threads, [owner_thread])
        finally:
            release.set()
            result = await task
        self.assertEqual(result.binding, context.binding)
        self.assertEqual([item.item_id for item in result.candidates], ["a", "b"])
        self.assertEqual(result.fallback_reason, "candidate_budget_truncated")

    async def test_cancellation_drains_real_cpu_before_returning(self):
        started, release = Event(), Event()

        def score(history, candidates):
            started.set()
            release.wait(2)
            return (2.0, 1.0)

        backend, context, command, _ = self.setup_backend(score)
        task = asyncio.create_task(backend.rank(context, command))
        try:
            await self.wait_started(started)
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done(), "cancellation released the live CPU work")
            self.assertEqual(backend.demo_queue.outstanding, 1)
        finally:
            release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertTrue(task.cancelled())
        self.assertEqual(backend.demo_queue.outstanding, 0)

    async def test_capacity_rejects_overload_without_unbounded_submission(self):
        started, release = Event(), Event()

        def score(history, candidates):
            started.set()
            release.wait(2)
            return (2.0, 1.0)

        backend, context, command, _ = self.setup_backend(score)
        await backend.demo_queue.aclose()
        backend.demo_queue = R06CPUQueue(workers=1, queued=0)
        first = asyncio.create_task(backend.rank(context, command))
        try:
            await self.wait_started(started)
            with self.assertRaises(ManagementError) as caught:
                await backend.rank(context, command)
            self.assertEqual(caught.exception.code, "demo_cpu_queue_full")
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(backend.demo_queue.outstanding, 1)
        finally:
            release.set()
            await first

    async def test_history_membership_uses_the_existing_index(self):
        class NoLinearScan(tuple):
            def __contains__(self, item):
                raise AssertionError("history membership scanned the complete item tuple")

        observed = []

        def score(history, candidates):
            observed.append((history, candidates))
            return (2.0, 1.0)

        backend, context, command, runtime = self.setup_backend(score)
        runtime.item_ids = NoLinearScan(runtime.item_ids)
        result = await backend.rank(context, command)
        self.assertEqual(observed, [(("a",), ("a", "b"))])
        self.assertEqual([item.score for item in result.candidates], [2.0, 1.0])

    async def test_closed_backend_rejects_new_demo_cpu_work(self):
        backend, context, command, _ = self.setup_backend(lambda *_: (2.0, 1.0))
        await backend.aclose()
        with self.assertRaises(ManagementError) as caught:
            await backend.rank(context, command)
        self.assertEqual(caught.exception.code, "demo_cpu_queue_closed")
