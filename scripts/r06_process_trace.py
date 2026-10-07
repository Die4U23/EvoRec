"""Opt-in, bounded per-request tracing for the owned process lab, not production."""

from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from unittest.mock import patch

from scripts.profile_r06_online import RequestTimings, observe


class ConcurrentTimings(RequestTimings):
    def __init__(self, samples):
        if type(samples) is not int or not 1 <= samples <= 122:
            raise ValueError("trace samples must be 1..122")
        self.samples = samples
        self.active = ContextVar("r06_diagnostic_request", default=None)
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
            request = dict(sample=sample, started=perf_counter(), stages=[], status_code=None, error_type=None)
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
def trace_api(timings):
    from evorec.api import app as api
    original = api.create_app
    with observe(timings), patch.object(api, "create_app", lambda *a, **kw: timings.wrap(original(*a, **kw))):
        yield
