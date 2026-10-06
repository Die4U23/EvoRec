"""Observe the existing isolated R06 verifier without changing its deadline.

This sequential diagnostic adds timing overhead. A diagnostic report, even when
its instrumented service passes, is not uninstrumented HTTP/browser/SLA proof.
Only static stage labels/times/statuses are recorded, never arguments or bodies.
"""

import argparse
from contextlib import ExitStack, contextmanager
from functools import wraps
import gc
import json
import os
from pathlib import Path
import subprocess
from threading import Lock
from time import perf_counter, thread_time
from unittest.mock import patch
from uuid import UUID

import httpx
import psycopg

from scripts import verify_r06_online as verifier
from scripts.assemble_r06_bundle import _source
from evorec.domain.errors import ManagementError, SnapshotMismatch
from evorec.infrastructure import postgres, r06_admission
from evorec.infrastructure.management import CatalogManager
from evorec.infrastructure.postgres import PostgresDemoBackend
from evorec.infrastructure.recommendation_execution import RecommendationExecution
from evorec.infrastructure.r06_async import R06CPUQueue
from evorec.infrastructure.r06_features import R06Features, PoolFeatures
from evorec.infrastructure.r06_retrieval import R06Retrieval
from evorec.infrastructure.r06_serving import R06SnapshotRanker


class RequestTimings:
    """One sequential API request at a time, including its drained worker threads."""

    def __init__(self):
        self.requests = []
        self.current = None
        self.lock = Lock()
        self.after_reset = False
        self._gc_started = None

    def gc(self, phase, info):
        """Observe collection duration only; never collect, disable or tune GC."""
        if phase == "start":
            self._gc_started = (self.current, info["generation"], perf_counter(), thread_time())
        elif phase == "stop" and self._gc_started is not None:
            request, generation, started, cpu_started = self._gc_started
            self._gc_started = None
            self._event(request, f"gc_generation_{generation}", started, cpu_started, None)

    def _event(self, request, label, started, cpu_started, error):
        if request is not None:
            event = dict(stage=label, start_seconds=started-request["started"],
                         wall_seconds=perf_counter()-started,
                         thread_cpu_seconds=None if cpu_started is None else thread_time()-cpu_started,
                         error_type=error)
            with self.lock:
                request["stages"].append(event)

    def sync(self, label, original):
        @wraps(original)
        def timed(*args, **kwargs):
            request, started, cpu_started, error = self.current, perf_counter(), thread_time(), None
            try:
                return original(*args, **kwargs)
            except BaseException as failure:
                error = type(failure).__name__
                raise
            finally:
                self._event(request, label, started, cpu_started, error)
        return timed

    def async_stage(self, label, original):
        @wraps(original)
        async def timed(*args, **kwargs):
            request, started, error = self.current, perf_counter(), None
            try:
                return await original(*args, **kwargs)
            except BaseException as failure:
                error = type(failure).__name__
                raise
            finally:
                self._event(request, label, started, None, error)
        return timed

    def post(self, original):
        @wraps(original)
        async def timed(client, url, *args, **kwargs):
            if not str(url).endswith("/api/v1/recommendations"):
                response = await original(client, url, *args, **kwargs)
                if str(url).endswith("/reset") and response.status_code == 200:
                    self.after_reset = True
                return response
            if self.current is not None:
                raise ValueError("diagnostic only supports sequential API requests")
            body = kwargs.get("json")
            value = body.get("strategy") if isinstance(body, dict) else None
            strategy = value if type(value) is str and value in {"popular", "dense", "adaptive"} else "other"
            request = dict(strategy=strategy, after_reset=self.after_reset, started=perf_counter(),
                           stages=[], status_code=None, error_type=None)
            self.current = request
            try:
                response = await original(client, url, *args, **kwargs)
                request["status_code"] = response.status_code
                return response
            except BaseException as failure:
                request["error_type"] = type(failure).__name__
                raise
            finally:
                request["wall_seconds"] = perf_counter()-request["started"]
                request["stages"].sort(key=lambda event: event["start_seconds"])
                self.requests.append(request)
                self.current = None
        return timed


@contextmanager
def observe(timings):
    sync_stages = (
        (CatalogManager, "ensure_ready", "publication_recovery"),
        (RecommendationExecution, "admit", "execution_lease_and_admission"),
        (PostgresDemoBackend, "_admit", "database_admission"),
        (PostgresDemoBackend, "_capture_context", "actual_catalog_read_and_capture"),
        (postgres, "capture_model", "actual_content_and_model_capture"),
        (r06_admission, "_seal", "catalog_seal"),
        (r06_admission, "restore_request", "persisted_snapshot_verification"),
        (R06SnapshotRanker, "score", "retrieval_and_ranking"),
        (R06Retrieval, "retrieve", "retrieval"),
        (R06Features, "build_pool", "candidate_features"),
        (PoolFeatures, "score", "candidate_ranking"),
        (PostgresDemoBackend, "_save", "result_write"),
        (PostgresDemoBackend, "_mark_failed", "failure_write"),
        (RecommendationExecution, "close", "execution_lease_close"),
    )
    with ExitStack() as stack:
        for target, name, label in sync_stages:
            stack.enter_context(patch.object(target, name, timings.sync(label, getattr(target, name))))
        for target, name, label in ((PostgresDemoBackend, "rank", "ranking_port"),
                                    (R06CPUQueue, "run", "queue_and_cpu_drain")):
            stack.enter_context(patch.object(target, name, timings.async_stage(label, getattr(target, name))))
        stack.enter_context(patch.object(httpx.AsyncClient, "post", timings.post(httpx.AsyncClient.post)))
        callback = timings.gc
        gc.callbacks.append(callback)
        stack.callback(gc.callbacks.remove, callback)
        yield


def _schemas(database_url):
    with psycopg.connect(database_url) as connection:
        return frozenset(row[0] for row in connection.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'test_evorec_%'"))


def profile(output, database_url, managed_root, bundle_id, digest, *, content_backend="stdlib",
            sample_profile=False, sample_reset=False):
    if sample_reset and not sample_profile:
        raise ValueError("sample reset requires the actual sample profile")
    project = Path(__file__).resolve().parents[1]
    output = Path(output).resolve()
    if not output.is_relative_to(project / "artifacts") or output == project / "artifacts":
        raise ValueError("diagnostic must use a fresh artifacts subdirectory")
    if output.exists():
        raise FileExistsError("diagnostic destination already exists")
    base = _source(project)
    before = _schemas(database_url)
    output.mkdir(parents=True, exist_ok=False)
    timings = RequestTimings()
    sources = (*verifier.SOURCE_FILES, "scripts/profile_r06_online.py", "tests/test_r06_profile.py")
    status, error = "passed", None
    with observe(timings), patch.object(verifier, "SOURCE_FILES", sources):
        try:
            verifier.verify(output / "service", database_url, managed_root, bundle_id, digest,
                            content_backend=content_backend, sample_profile=sample_profile, sample_reset=sample_reset)
        except (ValueError, OSError, psycopg.Error, subprocess.SubprocessError, ManagementError, SnapshotMismatch) as failure:
            status = "failed"
            error = dict(code=getattr(failure, "code", "verification_failed"), type=type(failure).__name__)
    if _source(project) != base:
        raise ValueError("source changed during diagnostic")
    restored = _schemas(database_url) == before
    result = dict(status="diagnostic_completed", service_status=status, service_error=error,
                  source_commit=base, source_working_tree_dirty=False, source_files_expected=len(sources),
                  content_backend=content_backend, ranker_backend=content_backend, sample_profile=sample_profile,
                  sample_reset=sample_reset,
                  api_deadline_seconds=2.0, deadline_increased=False,
                  temporary_schema_set_restored=restored,
                  requests=[{k: v for k, v in request.items() if k != "started"} for request in timings.requests],
                  timings_overlap_do_not_sum=True, instrumentation_overhead_not_subtracted=True,
                  uninstrumented_acceptance=False, browser_or_network_test=False)
    report, owned = output / "profile.json", False
    try:
        with report.open("xb") as stream:
            owned = True
            stream.write(json.dumps(result, sort_keys=True, allow_nan=False).encode("utf-8"))
    except BaseException:
        if owned:
            report.unlink(missing_ok=True)
        raise
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("managed_root", type=Path)
    parser.add_argument("bundle_id", type=UUID)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--content-backend", choices=("stdlib", "numpy"), default="stdlib")
    parser.add_argument("--sample-profile", action="store_true", help="diagnose the actual Demo fixed sample history")
    parser.add_argument("--sample-reset", action="store_true", help="diagnose sample feedback/reset and three fresh calculations")
    args = parser.parse_args(argv)
    url = os.getenv("EVOREC_DATABASE_URL")
    if not url:
        print(json.dumps(dict(status="failed", code="database_not_configured")))
        return 1
    try:
        result = profile(args.output, url, args.managed_root, args.bundle_id, args.expected_manifest_sha256,
                         content_backend=args.content_backend, sample_profile=args.sample_profile or args.sample_reset,
                         sample_reset=args.sample_reset)
    except (ValueError, OSError, psycopg.Error, subprocess.SubprocessError) as error:
        print(json.dumps(dict(status="failed", code=getattr(error, "code", "diagnostic_failed"),
                              error_type=type(error).__name__)))
        return 1
    print(json.dumps(result))
    return 0 if result["service_status"] == "passed" and result["temporary_schema_set_restored"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
