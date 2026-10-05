"""Real approved corpus service flow in an owned isolated PostgreSQL schema.

ASGI HTTP is not browser/network/SLA acceptance. Current-time service results
are not historical quality evaluation or the original research target scores.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
from time import perf_counter
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from evorec.api.app import create_app
from evorec.bootstrap import build_demo_application
from evorec.application.compare import ComparisonCommand
from evorec.domain.errors import ManagementError, SnapshotMismatch
from evorec.domain.models import Strategy
from evorec.infrastructure.postgres import PostgresDemoBackend
from scripts.assemble_r06_bundle import _source
from scripts.migrate_database import migrate
from scripts.verify_r06_catalog_preparation import SOURCE_FILES as CATALOG_SOURCES
from scripts.benchmark_r06_retrieval import _validation

SOURCE_FILES = tuple(dict.fromkeys((*CATALOG_SOURCES,
    "src/evorec/infrastructure/r06_async.py", "src/evorec/infrastructure/r06_admission.py",
    "src/evorec/infrastructure/comparison_store.py", "src/evorec/infrastructure/comparison_job.py",
    "src/evorec/application/recommend.py", "src/evorec/application/compare.py",
    "src/evorec/domain/models.py", "src/evorec/domain/recommendation.py",
    "src/evorec/domain/errors.py", "db/migrations/0010_r06_request_snapshot.sql",
    "scripts/comparison_worker.py", "scripts/verify_r06_online.py", "tests/test_r06_online.py")))
SOURCE_FILES = (*SOURCE_FILES, "src/evorec/infrastructure/_ranker_numpy.py", "tests/test_residual_ranker_numpy.py")


def verify(output, database_url, managed_root, bundle_id, digest, *, content_backend="stdlib"):
    project = Path(__file__).resolve().parents[1]
    output = Path(output).resolve()
    if not output.is_relative_to(project / "artifacts") or output == project / "artifacts":
        raise ValueError("verification must use a fresh artifacts subdirectory")
    if output.exists(): raise FileExistsError("verification destination already exists")
    base = _source(project)
    schema = "test_evorec_" + uuid4().hex
    isolated = make_conninfo(**{**conninfo_to_dict(database_url), "options": "-c search_path="+schema})
    output.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name in SOURCE_FILES:
        raw = (project / name).read_bytes()
        if raw != subprocess.check_output(["git", "show", f"{base}:{name}"], cwd=project):
            raise ValueError("working source differs from its Git blob")
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    created = False
    try:
        with psycopg.connect(database_url) as c:
            c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        created = True
        migrate(isolated)

        def application():
            return build_demo_application(PostgresDemoBackend(
                isolated, r06_enabled=True, r06_content_backend=content_backend, r06_ranker_backend=content_backend,
            ), managed_root=Path(managed_root))

        async def run():
            app = application()
            restarted = None
            try:
                prepared = await asyncio.to_thread(app.manager.prepare_r06_bundle, bundle_id, digest)
                await asyncio.to_thread(app.manager.publish, uuid4(), bundle_id, None)
                if not (await app.readiness.execute()).ready:
                    raise ValueError("isolated published service is not ready")
                bundle = app.backend.runtime.bundle
                raw = json.loads((Path(managed_root) / str(bundle_id) / "manifest.json").read_bytes())
                samples = _validation(Path(managed_root) / str(bundle_id) / "features", raw["components"]["features"])
                history = tuple(samples[0]["history"]) if content_backend == "numpy" else ()
                session = await app.backend.create_session()
                with app.backend._connect() as c:
                    c.execute("UPDATE sessions SET history=%s WHERE session_id=%s",
                              (Jsonb(list(history)), session.snapshot.session_id))
                body = dict(session_id=str(session.snapshot.session_id), expected_history_version=0, strategy="dense", k=10)
                headers = {"X-Session-Token": session.access_token, "Idempotency-Key": str(uuid4())}
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=app)),
                                             base_url="http://isolated") as client:
                    started = perf_counter()
                    first = await client.post("/api/v1/recommendations", json=body, headers=headers)
                    elapsed = perf_counter() - started
                    if first.status_code != 200:
                        raise ManagementError(first.json()["error"]["code"], "actual ASGI recommendation failed", first.status_code)
                    result = first.json()
                    if (result["model_version"] != bundle.model_version or result["actual_strategy"] != "dense"
                            or result["fallback_reason"] is not None or not result["items"]
                            or len({i["item_id"] for i in result["items"]}) != len(result["items"])
                            or any(i["source"] != "r06-a-frozen-s17" or i["item_id"] in history for i in result["items"])):
                        raise ValueError("actual API batch identity/legal items differ")
                    if (await client.post("/api/v1/recommendations", json=body, headers=headers)).json() != result:
                        raise ValueError("API idempotency replay differs")
                comparison = ComparisonCommand(uuid4(), session.snapshot.session_id, session.access_token, 0,
                                               (Strategy.POPULAR, Strategy.DENSE, Strategy.ADAPTIVE), 10, 60.)
                await app.comparison_jobs.enqueue(comparison)
                restarted = application()
                if not (await restarted.readiness.execute()).ready: raise ValueError("restart recovery failed")
                if not await restarted.comparison_jobs.run_next(): raise ValueError("frozen job did not execute")
                saved = await restarted.compare.get(comparison.comparison_id, comparison.session_id, comparison.session_token)
                if (saved.context.model.model_version != result["model_version"]
                        or saved.context.model.full_seen != frozenset(history)
                        or saved.strategies[1].actual_strategy != Strategy.DENSE
                        or saved.strategies[1].items != saved.strategies[2].items
                        or await restarted.compare.save(comparison) != saved):
                    raise ValueError("frozen job identity/replay differs")
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=restarted)),
                                             base_url="http://isolated") as client:
                    replay = await client.post("/api/v1/recommendations", json=body, headers=headers)
                    if replay.status_code != 200 or replay.json() != result: raise ValueError("restart API replay differs")
                if app.backend.r06_queue.outstanding or restarted.backend.r06_queue.outstanding:
                    raise ValueError("CPU work did not drain")
                return dict(item_count=prepared["item_count"], model_version=bundle.model_version,
                            history_count=len(history), returned_items=len(result["items"]),
                            single_asgi_request_seconds=elapsed,
                            actual_asgi_recommendation=True, adaptive_resolves_dense=True,
                            api_idempotency_exact=True, restart_recovery_exact=True,
                            frozen_background_job_replay_exact=True, cpu_jobs_drained=True)
            finally:
                await app.backend.aclose()
                if restarted is not None: await restarted.backend.aclose()

        result = asyncio.run(run())
        if _source(project) != base or any(hashlib.sha256((project/n).read_bytes()).hexdigest() != h for n,h in hashes.items()):
            raise ValueError("source changed during service verification")
    finally:
        if created:
            with psycopg.connect(database_url) as c:
                c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    result.update(status="passed", activated_only_in_owned_schema=True, business_schema_untouched=True,
                  content_backend=content_backend, ranker_backend=content_backend,
                  bundle_id=str(bundle_id), manifest_sha256=digest,
                  owned_temporary_schema_removed=True, browser_tested=False, network_server_tested=False,
                  sla_proven=False, test_targets_evaluated=False, retrained=False,
                  source=dict(base_commit=base, working_tree_dirty=False, source_sha256=hashes))
    report, owned = output / "verification.json", False
    try:
        with report.open("xb") as stream:
            owned = True
            stream.write(json.dumps(result, sort_keys=True, allow_nan=False).encode("utf-8"))
    except BaseException:
        if owned: report.unlink(missing_ok=True)
        raise
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("managed_root", type=Path)
    parser.add_argument("bundle_id", type=UUID)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--content-backend", choices=("stdlib", "numpy"), default="stdlib")
    args = parser.parse_args(argv)
    url = os.getenv("EVOREC_DATABASE_URL")
    if not url:
        print(json.dumps(dict(status="failed", code="database_not_configured")))
        return 1
    try:
        result = verify(args.output, url, args.managed_root, args.bundle_id, args.expected_manifest_sha256,
                        content_backend=args.content_backend)
    except (ValueError, OSError, psycopg.Error, subprocess.SubprocessError, ManagementError, SnapshotMismatch) as error:
        print(json.dumps(dict(status="failed", code=getattr(error, "code", "verification_failed"),
                              error_type=type(error).__name__)))
        return 1
    print(json.dumps({k:v for k,v in result.items() if k != "source"}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
