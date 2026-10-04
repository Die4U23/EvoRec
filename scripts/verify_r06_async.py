"""Replay original fixed references through the actual bounded async RankingPort."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
from uuid import NAMESPACE_URL, uuid5

from scripts.assemble_r06_bundle import _source
from scripts.benchmark_r06_retrieval import _validation
from scripts.verify_r06_bundle import SOURCE_FILES as BUNDLE_SOURCES
from evorec.domain.errors import ManagementError, SnapshotMismatch
from evorec.domain.models import CatalogSnapshot, RecommendationCommand, RequestContext, SessionSnapshot, Strategy
from evorec.domain.recommendation import select_results
from evorec.infrastructure.r06_async import R06CPUQueue, R06RankingPort
from evorec.infrastructure.r06_bundle import load_r06_bundle
from evorec.infrastructure.r06_catalog import source_records
from evorec.infrastructure.residual_ranker import SCORE_TOLERANCE, _json, _read

SOURCE_FILES = (*BUNDLE_SOURCES, "src/evorec/infrastructure/r06_async.py", "scripts/verify_r06_async.py",
                "tests/test_r06_async.py", "src/evorec/application/ports.py", "src/evorec/domain/models.py",
                "src/evorec/domain/recommendation.py", "src/evorec/domain/errors.py",
                ".github/workflows/service-integration.yml", "src/evorec/infrastructure/r06_catalog.py")


def verify(output, managed_root, bundle_dir, digest, *, content_backend="stdlib"):
    project = Path(__file__).resolve().parents[1]
    output = Path(output).resolve()
    if not output.is_relative_to(project / "artifacts") or output == project / "artifacts":
        raise ValueError("async verification must use a fresh artifacts subdirectory")
    if output.exists(): raise FileExistsError("verification destination already exists")
    base = _source(project)
    bundle = load_r06_bundle(managed_root, bundle_dir, expected_manifest_sha256=digest, content_backend=content_backend)
    root = Path(bundle_dir).resolve()
    records = source_records(root, bundle)
    raw_manifest = _read(root, "manifest.json", 16*1024)
    if hashlib.sha256(raw_manifest).hexdigest() != digest:
        raise ValueError("bundle approval changed during async verification")
    manifest = _json(raw_manifest)
    samples = _validation(root / "features", manifest["components"]["features"])
    references = _validation(root / "ranker", manifest["components"]["ranker"])
    if not samples or len(samples) != len(references): raise ValueError("fixed references differ")
    output.mkdir(parents=True, exist_ok=False)
    report, owned = output / "verification.json", False
    try:
        hashes = {}
        for name in SOURCE_FILES:
            raw = (project / name).read_bytes()
            target = output / "source" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()

        async def run():
            queue = R06CPUQueue(workers=1, queued=8)
            try:
                ports, commands = [], []
                for index,sample in enumerate(samples):
                    identity = uuid5(NAMESPACE_URL, f"evorec/r06-async/{digest}/{index}")
                    context = RequestContext(identity, SessionSnapshot(identity, 0, 0, tuple(sample["history"]), frozenset()),
                                             CatalogSnapshot(bundle.bundle_id, 0, frozenset(bundle.adapter.features.item_ids)))
                    # Offline construction from approved source records, not DB admission.
                    ports.append(R06RankingPort(queue, bundle, context, timestamp_ms=sample["timestamp_ms"],
                                               full_seen=frozenset(sample["seen"]), catalog_items=records))
                    commands.append(RecommendationCommand(identity, identity, "offline-fixed-reference", 0, Strategy.DENSE, 20, 60.))
                results = await asyncio.gather(*(p.rank(p.request.context, c) for p,c in zip(ports, commands, strict=True)),
                                               return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException): raise result
                if queue.outstanding != 0: raise ValueError("ranking jobs did not drain")
                return ports, results
            finally: await queue.aclose()

        ports, batches = asyncio.run(run())
        maximum, rows = 0., []
        for index,(port,batch,sample,reference) in enumerate(zip(ports,batches,samples,references,strict=True)):
            error = max((abs(c.score-score) for c,score in zip(batch.candidates,reference["expected_scores"],strict=True)), default=0.)
            if (batch.binding != port.request.context.binding or port.model_version != bundle.model_version
                    or batch.actual_strategy != Strategy.DENSE or batch.fallback_reason is not None
                    or tuple(c.item_id for c in batch.candidates) != tuple(sample["expected_items"])
                    or any(c.source != "r06-a-frozen-s17" for c in batch.candidates) or error > SCORE_TOLERANCE
                    or tuple(c.item_id for c in select_results(batch.candidates,port.request.context,20)) !=
                       tuple(sample["expected_items"][i] for i in reference["expected_top20"])):
                raise ValueError("actual async batch, version, score or legal Top-20 differs")
            maximum = max(maximum,error)
            rows.append(dict(row=index,candidate_count=len(batch.candidates)))
        if _source(project) != base or any(hashlib.sha256((project / n).read_bytes()).hexdigest()!=h for n,h in hashes.items()):
            raise ValueError("source changed during async verification")
        result = dict(status="passed", activated=False, content_backend=content_backend,
                      bundle_id=str(bundle.bundle_id), manifest_sha256=digest, model_version=bundle.model_version,
                      actual_ranking_port_batches_exact=True, legal_top20_exact=True, rows=rows, max_score_error=maximum,
                      queued_and_running_capacity=9, workers=1, ranking_jobs_drained=True,
                      database_admission=False, test_queries_evaluated=False, retrained=False,
                      source=dict(base_commit=base, working_tree_dirty=False, source_sha256=hashes))
        with report.open("xb") as stream:
            owned = True
            stream.write(json.dumps(result,sort_keys=True,allow_nan=False).encode("utf-8"))
        return result
    except BaseException:
        if owned: report.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("managed_root", type=Path)
    parser.add_argument("bundle_dir", type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--content-backend", choices=("stdlib", "numpy"), default="stdlib")
    args = parser.parse_args(argv)
    try:
        result = verify(args.output,args.managed_root,args.bundle_dir,args.expected_manifest_sha256,
                        content_backend=args.content_backend)
    except (ValueError, OSError, subprocess.SubprocessError, ManagementError, SnapshotMismatch) as error:
        print(json.dumps(dict(status="failed",code=getattr(error,"code","verification_failed"),error_type=type(error).__name__)))
        return 1
    print(json.dumps({k:v for k,v in result.items() if k!="source"}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
