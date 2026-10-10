"""Opt-in, bounded per-request tracing for the owned process lab, not production."""

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import Lock
from time import perf_counter
from unittest.mock import patch

import psycopg

from scripts.profile_r06_online import RequestTimings, observe


def query_kind(query):
    """Return fixed labels only, never persist SQL, parameters or their hashes."""
    if type(query) is not str or len(query) > 4096:
        return "other"
    statement = " ".join(query.split())
    for fragment, label in (
        ("FROM sessions WHERE session_id = %s FOR SHARE", "session_snapshot_lock"),
        ("FROM recommendation_requests WHERE request_id = %s FOR UPDATE", "request_row_lock"),
        ("FROM catalog_control WHERE singleton = 1 AND admission_open FOR SHARE", "catalog_barrier"),
        ("FROM bundle_items bi JOIN items i ON i.item_id=bi.item_id", "actual_catalog_rows"),
        ("FROM bounded_members bi JOIN items i ON i.item_id=bi.item_id", "actual_catalog_rows"),
        ("INSERT INTO recommendation_requests", "accepted_insert"),
        ("SELECT pg_advisory_lock(", "publication_lock"),
        ("SELECT pg_try_advisory_lock(", "execution_lock"),
    ):
        if fragment in statement:
            return label
    return "other"


class ConcurrentTimings(RequestTimings):
    def __init__(self, samples, *, phase_gate=False):
        if type(samples) is not int or not 1 <= samples <= 122:
            raise ValueError("trace samples must be 1..122")
        if type(phase_gate) is not bool:
            raise ValueError("phase_gate must be a bool")
        self.phase_gate = phase_gate
        self._phase_gate_lock = Lock()
        self._timeline_origin = perf_counter()
        self.samples = samples
        self.active = ContextVar("r06_diagnostic_request", default=None)
        self.phase = ContextVar("r06_diagnostic_phase", default="other")
        self.seen = set()
        super().__init__()

    @property
    def current(self):
        return self.active.get()

    @current.setter
    def current(self, value):
        self.active.set(value)

    def post(self, original):
        # The request boundary is real ASGI, not a patched HTTP client.
        return original

    def sync(self, label, original):
        timed = super().sync(label, original)
        @wraps(original)
        def scoped(*args, **kwargs):
            token = self.phase.set(label)
            try:
                return timed(*args, **kwargs)
            finally:
                self.phase.reset(token)
        if self.phase_gate and label in {"actual_catalog_read_and_capture", "retrieval_and_ranking"}:
            @wraps(original)
            def gated(*args, **kwargs):
                request = self.current
                if request is None:
                    return scoped(*args, **kwargs)
                started = perf_counter()
                with self._phase_gate_lock:
                    # Diagnostic intervention only. Keep waiting OUTSIDE the
                    # original business span; cancellation/drain stay unchanged.
                    self._event(request, label + "_diagnostic_gate_wait", started, None, None)
                    return scoped(*args, **kwargs)
            return gated
        return scoped

    def database(self, operation, original):
        phases = {"publication_recovery", "execution_lease_and_admission", "database_admission",
                  "actual_catalog_read_and_capture", "actual_content_and_model_capture",
                  "result_write", "failure_write", "execution_lease_close"}
        @wraps(original)
        def timed(*args, **kwargs):
            if self.current is None:
                return original(*args, **kwargs)
            phase = self.phase.get()
            phase = phase if phase in phases else "other"
            kind = query_kind(args[1] if len(args) > 1 else kwargs.get("query")) if operation == "execute" else ""
            label = f"{phase}_database_{operation}" + (f"_{kind}" if kind else "")
            # Use the base timer: an inner driver call must not change the
            # containing business phase or replace arguments/results/errors.
            return super(ConcurrentTimings, self).sync(label, original)(*args, **kwargs)
        return timed

    def async_stage(self, label, original):
        if label != "queue_and_cpu_drain":
            return super().async_stage(label, original)

        async def timed(queue, work):
            request, started = self.current, perf_counter()
            def bound_work():
                # ThreadPoolExecutor does not propagate ContextVars itself.
                token = self.active.set(request)
                try:
                    self._event(request, "cpu_queue_wait", started, None, None)
                    return work()
                finally:
                    self.active.reset(token)
            return await super(ConcurrentTimings, self).async_stage(label, original)(queue, bound_work)
        return timed

    def wrap(self, app):
        async def traced(scope, receive, send):
            headers = [value for key, value in scope.get("headers", []) if key == b"x-evorec-diagnostic-sample"]
            if (scope.get("type") != "http" or scope.get("method") != "POST"
                    or scope.get("path") != "/api/v1/recommendations" or len(headers) != 1
                    or len(headers[0]) > 3 or not headers[0].isdigit()):
                return await app(scope, receive, send)
            sample = int(headers[0])
            if sample >= self.samples or str(sample).encode("ascii") != headers[0]:
                return await app(scope, receive, send)
            with self.lock:
                if sample in self.seen:
                    duplicate = True
                else:
                    self.seen.add(sample)
                    duplicate = False
            if duplicate:
                return await app(scope, receive, send)
            started = perf_counter()
            request = dict(sample=sample, asgi_start_offset_seconds=started - self._timeline_origin,
                           started=started, stages=[], status_code=None, error_type=None)
            token = self.active.set(request)
            async def observed_send(message):
                if message.get("type") == "http.response.start":
                    request["status_code"] = message["status"]
                await send(message)
            try:
                return await app(scope, receive, observed_send)
            except BaseException:
                request["error_type"] = "asgi_exception"
                raise
            finally:
                request["wall_seconds"] = perf_counter() - request["started"]
                self.active.reset(token)
                with self.lock:
                    self.requests.append(request)
        return traced

    def report(self):
        return [dict((key, value) for key, value in request.items() if key != "started")
                | {"stages": sorted(request["stages"], key=lambda event: event["start_seconds"])}
                for request in sorted(self.requests, key=lambda request: request["sample"])]


@contextmanager
def trace_api(timings, *, gc_events=True):
    from evorec.api import app as api
    original = api.create_app
    with observe(timings, gc_events=gc_events), ExitStack() as stack:
        stack.enter_context(patch.object(api, "create_app", lambda *a, **kw: timings.wrap(original(*a, **kw))))
        stack.enter_context(patch.object(psycopg, "connect", timings.database("connect", psycopg.connect)))
        for target, method, label in (
            (psycopg.Cursor, "execute", "execute"),
            (psycopg.Cursor, "fetchall", "fetchall_decode"),
            (psycopg.Cursor, "fetchone", "fetchone_decode"),
            (psycopg.Cursor, "close", "cursor_close"),
            (psycopg.Connection, "__exit__", "transaction_exit"),
            # Explicit transaction() commits/rolls back without closing the
            # lease connection; Connection.__exit__ alone cannot observe it.
            (psycopg.Transaction, "__exit__", "transaction_context_exit"),
            (psycopg.Connection, "commit", "commit"),
            (psycopg.Connection, "rollback", "rollback"),
            (psycopg.Connection, "close", "close"),
        ):
            stack.enter_context(patch.object(target, method, timings.database(label, getattr(target, method))))
        yield
