"""Replay an approved package using original fixed references and actual texts."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from uuid import NAMESPACE_URL, uuid5

from scripts.assemble_r06_bundle import _source
from scripts.benchmark_r06_retrieval import _validation, _verify
from scripts.verify_r06_serving import SOURCE_FILES as SERVING_SOURCES
from evorec.domain.models import CatalogSnapshot, RequestContext, SessionSnapshot
from evorec.domain.recommendation import select_results
from evorec.infrastructure.r06_bundle import FrozenCatalogItem, KIND, load_r06_bundle
from evorec.infrastructure.r06_features import MAX_JSON_BYTES, _json
from evorec.infrastructure.residual_ranker import SCORE_TOLERANCE, _read, _verified

SOURCE_FILES = (*SERVING_SOURCES, "src/evorec/infrastructure/content_encoder.py",
                "src/evorec/infrastructure/r06_bundle.py", "scripts/assemble_r06_bundle.py",
                "scripts/load_r06_bundle.py", "scripts/verify_r06_bundle.py", "tests/test_r06_bundle.py")


def verify(output, managed_root, bundle_dir, digest, *, content_backend="stdlib"):
    project = Path(__file__).resolve().parents[1]
    output = Path(output).resolve()
    if not output.is_relative_to(project / "artifacts") or output == project / "artifacts":
        raise ValueError("replay must use a new artifacts subdirectory")
    if output.exists():
        raise FileExistsError("replay destination already exists")
    base = _source(project)
    bundle = load_r06_bundle(managed_root, bundle_dir, expected_manifest_sha256=digest, content_backend=content_backend)
    root = Path(bundle_dir).resolve()
    raw_manifest = _read(root, "manifest.json", 16*1024)
    if hashlib.sha256(raw_manifest).hexdigest() != digest:
        raise ValueError("bundle approval changed during replay")
    manifest = _json(raw_manifest)
    catalog = _json(_verified(root, manifest["catalog"], "catalog.json", MAX_JSON_BYTES))
    metadata = _json(_verified(root, manifest["metadata"], "metadata.json", MAX_JSON_BYTES))
    records = tuple(FrozenCatalogItem(item, metadata.get(item, ""), catalog[item]) for item in bundle.adapter.features.item_ids)
    samples = _validation(root / "features", manifest["components"]["features"])
    references = _validation(root / "ranker", manifest["components"]["ranker"])
    if len(samples) != len(references) or not samples:
        raise ValueError("joint references must be nonempty and have equal counts")
    output.mkdir(parents=True, exist_ok=False)
    report, owned = output / "verification.json", False
    try:
        hashes = {}
        for relative in SOURCE_FILES:
            raw = (project / relative).read_bytes()
            target = output / "source" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            hashes[relative] = hashlib.sha256(raw).hexdigest()
        maximum, rows = [0., 0., 0.], []
        for row, (sample, reference) in enumerate(zip(samples, references, strict=True)):
            identity = uuid5(NAMESPACE_URL, f"evorec/r06-bundle-replay/{digest}/{row}")
            context = RequestContext(identity, SessionSnapshot(identity, 0, 0, tuple(sample["history"]), frozenset()),
                                      CatalogSnapshot(bundle.bundle_id, 0, frozenset(bundle.adapter.features.item_ids)))
            actual = bundle.score(context, sample["timestamp_ms"], frozenset(sample["seen"]), records)
            errors = _verify(bundle.adapter.features, bundle.adapter.ranker, actual.retrieval, sample, reference)
            candidates = actual.batch.candidates
            batch_error = max((abs(c.score-score) for c, score in
                               zip(candidates, reference["expected_scores"], strict=True)), default=0.)
            if (actual.batch.binding != context.binding or actual.model_version != bundle.model_version
                    or tuple(c.item_id for c in candidates) != tuple(sample["expected_items"])
                    or batch_error > SCORE_TOLERANCE
                    or tuple(c.item_id for c in select_results(candidates, context, 20)) !=
                       tuple(sample["expected_items"][i] for i in reference["expected_top20"])):
                raise ValueError("packaged batch, version, scores or legal Top-20 changed")
            maximum = [max(a, b) for a, b in zip(maximum, (*errors[:2], max(errors[2], batch_error)), strict=True)]
            rows.append(dict(row=row, candidate_count=len(candidates),
                             provider_counts=[len(actual.retrieval.collaborative), len(actual.retrieval.content)]))
        if _source(project) != base or any(hashlib.sha256((project / name).read_bytes()).hexdigest() != h for name, h in hashes.items()):
            raise ValueError("source changed during replay")
        result = dict(status="passed", kind=KIND, activated=False, content_backend=content_backend,
                      bundle_id=str(bundle.bundle_id), manifest_sha256=digest, model_version=bundle.model_version,
                      assembly_revision=manifest["assembly_revision"], rows=rows, actual_catalog_items_checked=len(records),
                      full_provider_orders_exact=True, request_bindings_exact=True, legal_top20_exact=True,
                      max_context_error=maximum[0], max_scalar_error=maximum[1], max_score_error=maximum[2],
                      retrained=False, test_queries_evaluated=False,
                      source=dict(base_commit=base, working_tree_dirty=False, source_sha256=hashes))
        with report.open("xb") as stream:
            owned = True
            stream.write(json.dumps(result, sort_keys=True, allow_nan=False).encode("utf-8"))
        return result
    except BaseException:
        if owned:
            report.unlink(missing_ok=True)
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
        result = verify(args.output, args.managed_root, args.bundle_dir, args.expected_manifest_sha256,
                        content_backend=args.content_backend)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps(dict(status="failed", code=getattr(error, "code", "verification_failed"), message=str(error))))
        return 1
    print(json.dumps({key: value for key, value in result.items() if key != "source"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
