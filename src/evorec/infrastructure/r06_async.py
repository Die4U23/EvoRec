"""Bounded cross-event-loop CPU ranking, not database admission or activation.

Cancellation removes queued work but cannot interrupt a running Python thread.
The caller drains that real computation before releasing its captured bundle.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock

from evorec.domain.errors import ManagementError, SnapshotMismatch
from evorec.domain.models import RankedBatch, RecommendationCommand, RequestContext, Strategy
from evorec.infrastructure.r06_bundle import FrozenR06Bundle
from evorec.infrastructure.r06_serving import FrozenR06Request, R06ServingResult


async def _drain(waiter):
    """Finish underlying work even after repeated caller cancellation; discard it."""
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if not waiter.cancelled():
        waiter.exception()  # Consume discarded failures; no unobserved-future warnings.


class R06CPUQueue:
    """One explicit runtime-owned pool; capacity includes running and queued jobs.

    A thread lock, rather than an asyncio semaphore, permits API and background
    worker event loops to share the same pool. No database writes occur here.
    """

    def __init__(self, *, workers=1, queued=8):
        if type(workers) is not int or not 1 <= workers <= 4:
            raise ValueError("workers must be an integer between 1 and 4")
        if type(queued) is not int or not 0 <= queued <= 32:
            raise ValueError("queued capacity must be an integer between 0 and 32")
        self._capacity = workers + queued
        self._lock = Lock()
        self._closed = False
        self._jobs = set()
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="evorec-r06")

    @property
    def outstanding(self):
        with self._lock:
            return len(self._jobs)

    @property
    def capacity(self):
        return self._capacity

    def _finished(self, future):
        with self._lock:
            self._jobs.discard(future)

    def _submit(self, work):
        if not callable(work):
            raise TypeError("CPU work must be callable")
        with self._lock:
            if self._closed:
                raise ManagementError("r06_queue_closed", "R06 ranking pool is closed", 503)
            if len(self._jobs) >= self._capacity:
                raise ManagementError("r06_queue_full", "R06 ranking capacity is exhausted", 429)
            try:
                future = self._executor.submit(work)
            except RuntimeError as error:
                raise ManagementError("r06_queue_unavailable", "R06 ranking pool is unavailable", 503) from error
            self._jobs.add(future)
        # A completed future invokes callbacks inline. Never hold our lock here.
        future.add_done_callback(self._finished)
        return future

    async def run(self, work):
        future = self._submit(work)
        waiter = asyncio.wrap_future(future)
        try:
            return await asyncio.shield(waiter)
        except asyncio.CancelledError:
            future.cancel()  # True only while queued; running CPU is not killed.
            await _drain(waiter)
            raise

    def _stop(self):
        with self._lock:
            first = not self._closed
            self._closed = True
            jobs = tuple(self._jobs)
        if first:
            self._executor.shutdown(wait=False, cancel_futures=True)
        return jobs

    def close(self):
        """Reject new work and cancel queued work; running jobs still retain leases."""
        self._stop()

    async def aclose(self):
        """Drain running jobs before acknowledging closure, including on cancellation."""
        jobs = self._stop()
        waiter = asyncio.gather(*(asyncio.wrap_future(job) for job in jobs), return_exceptions=True)
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            await _drain(waiter)
            raise


@dataclass(frozen=True, init=False)
class R06RankingPort:
    """A per-request port capturing an approved object and actual catalog records.

    Construction validates all actual text/time records synchronously and belongs
    in a trusted admission thread, not the HTTP event loop. This does not produce
    or persist a database snapshot and does not authorize client-provided records.
    """

    queue: R06CPUQueue
    bundle: FrozenR06Bundle
    request: FrozenR06Request

    def __init__(self, queue, bundle, context, *, timestamp_ms, full_seen, catalog_items):
        if type(queue) is not R06CPUQueue or type(bundle) is not FrozenR06Bundle:
            raise TypeError("a runtime-owned CPU pool and approved frozen bundle are required")
        captured = bundle.request(context, timestamp_ms, full_seen, catalog_items)
        object.__setattr__(self, "queue", queue)
        object.__setattr__(self, "bundle", bundle)
        object.__setattr__(self, "request", captured)

    @property
    def model_version(self):
        return self.bundle.model_version

    def _score(self):
        result = self.bundle.adapter.score(self.request)
        if (type(result) is not R06ServingResult or type(result.batch) is not RankedBatch
                or result.batch.binding != self.request.context.binding
                or result.batch.actual_strategy != Strategy.DENSE or result.batch.fallback_reason is not None
                or result.model_version != self.bundle.adapter.model_version
                or result.timestamp_ms != self.request.timestamp_ms):
            raise SnapshotMismatch("R06 CPU result differs from the captured request or model")
        return result.batch

    async def rank(self, context, command):
        if (type(context) is not RequestContext or type(command) is not RecommendationCommand
                or context != self.request.context
                or command.request_id != context.request_id
                or command.session_id != context.session.session_id
                or command.expected_history_version != context.session.history_version):
            raise SnapshotMismatch("R06 port was called with a different admitted snapshot")
        if command.strategy != Strategy.DENSE:
            raise ManagementError("r06_strategy_not_supported", "this frozen port only executes dense R06 ranking", 422)
        return await self.queue.run(self._score)
