"""Replay frozen snapshot adaptation on approved original references; no activation."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from uuid import NAMESPACE_URL, uuid5

from scripts.benchmark_r06_retrieval import SOURCE_FILES as RETRIEVAL_SOURCES, _validation, _verify
from evorec.domain.models import CatalogSnapshot, RequestContext, SessionSnapshot
from evorec.domain.recommendation import select_results
from evorec.infrastructure.r06_features import load_r06_features
from evorec.infrastructure.r06_retrieval import load_r06_retrieval
from evorec.infrastructure.r06_serving import FrozenR06Request, R06SnapshotRanker, SERVING_POLICY
from evorec.infrastructure.residual_ranker import ControlledLoadError, SCORE_TOLERANCE, load_residual_ranker

SOURCE_FILES = (*RETRIEVAL_SOURCES, "src/evorec/infrastructure/r06_serving.py",
                "scripts/verify_r06_serving.py", "tests/test_r06_serving.py")


def verify(output, features_component, features_digest, retrieval_component, retrieval_digest,
           ranker_component, ranker_digest, *, content_backend="stdlib"):
    project = Path(__file__).resolve().parents[1]
    output = Path(output).resolve()
    if not output.is_relative_to(project / "artifacts") or output == project / "artifacts":
        raise ValueError("replay must use a new artifacts subdirectory")
    if output.exists():
        raise FileExistsError("replay destination already exists")
    base = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    if subprocess.check_output(["git", "-C", str(project), "status", "--porcelain"], text=True).strip():
        raise ControlledLoadError("source_dirty", "commit implementation before recorded replay")
    features = load_r06_features(features_component, expected_manifest_sha256=features_digest)
    retrieval = load_r06_retrieval(retrieval_component, features, expected_manifest_sha256=retrieval_digest,
                                  content_backend=content_backend)
    ranker = load_residual_ranker(ranker_component, expected_manifest_sha256=ranker_digest)
    bundle_id = uuid5(NAMESPACE_URL, "evorec/r06-component-replay/"+features_digest+retrieval_digest+ranker_digest)
    adapter = R06SnapshotRanker(bundle_id, features, retrieval, ranker)
    samples, references = _validation(features_component, features_digest), _validation(ranker_component, ranker_digest)
    if len(samples) != len(references):
        raise ValueError("feature and ranker reference counts differ")
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
        rows, maximum = [], [0., 0., 0.]
        for row, (sample, reference) in enumerate(zip(samples, references, strict=True)):
            identity = uuid5(bundle_id, str(row))
            context = RequestContext(identity, SessionSnapshot(identity, 0, 0, tuple(sample["history"]), frozenset()),
                                      CatalogSnapshot(bundle_id, 0, frozenset(features.item_ids)))
            request = FrozenR06Request(context, sample["timestamp_ms"], frozenset(sample["seen"]),
                                        features.manifest_sha256, features.provenance["catalog_sha256"])
            actual = adapter.score(request)
            errors = _verify(features, ranker, actual.retrieval, sample, reference)
            candidates = actual.batch.candidates
            batch_error = max((abs(c.score-score) for c, score in
                               zip(candidates, reference["expected_scores"], strict=True)), default=0.)
            if (actual.batch.binding != context.binding
                    or tuple(c.item_id for c in candidates) != tuple(sample["expected_items"])
                    or batch_error > SCORE_TOLERANCE
                    or tuple(c.item_id for c in select_results(candidates, context, 20)) !=
                       tuple(sample["expected_items"][i] for i in reference["expected_top20"])):
                raise ValueError("serving batch binding, scores or legal Top-20 changed")
            errors = (*errors[:2], max(errors[2], batch_error))
            maximum = [max(a, b) for a, b in zip(maximum, errors, strict=True)]
            rows.append({"row": row, "candidate_count": len(candidates),
                         "provider_counts": [len(actual.retrieval.collaborative), len(actual.retrieval.content)]})
        if any(hashlib.sha256((project / name).read_bytes()).hexdigest() != digest for name, digest in hashes.items()):
            raise ValueError("source changed during replay")
        result = dict(status="passed", component_only=True, activated=False, serving_policy=SERVING_POLICY,
                      content_backend=content_backend, model_version=adapter.model_version, rows=rows,
                      request_bindings_exact=True, full_provider_orders_exact=True, legal_top20_exact=True,
                      max_context_error=maximum[0], max_scalar_error=maximum[1], max_score_error=maximum[2],
                      features_manifest_sha256=features_digest, retrieval_manifest_sha256=retrieval_digest,
                      ranker_manifest_sha256=ranker_digest, test_queries_evaluated=False, retrained=False,
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
    for kind in ("features", "retrieval", "ranker"):
        parser.add_argument(f"--{kind}-component", type=Path, required=True)
        parser.add_argument(f"--expected-{kind}-manifest-sha256", required=True)
    parser.add_argument("--content-backend", choices=("stdlib", "numpy"), default="stdlib")
    args = parser.parse_args(argv)
    try:
        result = verify(args.output, args.features_component, args.expected_features_manifest_sha256,
                        args.retrieval_component, args.expected_retrieval_manifest_sha256,
                        args.ranker_component, args.expected_ranker_manifest_sha256, content_backend=args.content_backend)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "code": getattr(error, "code", "verification_failed"), "message": str(error)}))
        return 1
    print(json.dumps({key: value for key, value in result.items() if key != "source"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
