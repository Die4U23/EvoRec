"""Hash-pinned ABBA/BAAB retrieval comparison; no activation, labels or refitting."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

from evorec.infrastructure.r06_features import FEATURE_TOLERANCE, MAX_JSON_BYTES, _json, load_r06_features
from evorec.infrastructure.r06_retrieval import load_r06_retrieval
from evorec.infrastructure.residual_ranker import SCORE_TOLERANCE, _read, _verified, load_residual_ranker

SOURCE_FILES = (
    "src/evorec/infrastructure/_content_numpy.py", "src/evorec/infrastructure/r06_retrieval.py",
    "src/evorec/infrastructure/r06_features.py", "src/evorec/infrastructure/residual_ranker.py",
    "src/evorec/infrastructure/model_runtime.py", "src/evorec/infrastructure/bundle.py",
    "scripts/load_r06_retrieval.py", "scripts/benchmark_r06_retrieval.py",
    "tests/test_r06_retrieval_numpy.py", "tests/test_r06_retrieval_runtime.py",
    "tests/test_r06_retrieval_benchmark.py", "requirements-retrieval.lock.txt", "pyproject.toml",
)


def _validation(root, digest):
    root = Path(root).resolve()
    raw = _read(root, "manifest.json", MAX_JSON_BYTES)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("approved manifest changed during comparison")
    return _json(_verified(root, _json(raw)["validation"], "validation.json", MAX_JSON_BYTES))


def _verify(features, ranker, result, sample, reference):
    if (result.collaborative != tuple(sample["collaborative"]) or result.content != tuple(sample["content"])):
        raise ValueError("full provider order changed")
    pool = features.build_pool(sample["history"], sample["seen"], sample["timestamp_ms"], result.collaborative, result.content)
    if pool.item_ids != tuple(sample["expected_items"]):
        raise ValueError("archived candidate union changed")
    context_error = max(abs(a-b) for a,b in zip(pool.context, sample["expected_context"], strict=True))
    scalar_error = max((abs(a-b) for actual, expected in zip(pool.scalars, sample["expected_scalars"], strict=True)
                        for a,b in zip(actual, expected, strict=True)), default=0.)
    scores = pool.score(ranker)
    if not all(math.isfinite(value) for value in scores):
        raise ValueError("ranker returned non-finite scores")
    error = max(abs(a-b) for a,b in zip(scores, reference["expected_scores"], strict=True))
    if (context_error > FEATURE_TOLERANCE or scalar_error > FEATURE_TOLERANCE or error > SCORE_TOLERANCE
            or sorted(range(len(scores)), key=lambda i: -scores[i])[:20] != reference["expected_top20"]):
        raise ValueError("archived context/scalars/scores or Top-20 changed")
    return context_error, scalar_error, error


def benchmark(output, features_component, features_digest, retrieval_component, retrieval_digest,
              ranker_component, ranker_digest, *, rounds=1):
    if type(rounds) is not int or not 1 <= rounds <= 3:
        raise ValueError("one to three fixed comparison rounds are supported")
    project = Path(__file__).resolve().parents[1]
    output = Path(output).resolve()
    if not output.is_relative_to(project / "artifacts") or output == project / "artifacts":
        raise ValueError("comparison must use a new artifacts subdirectory")
    if output.exists():
        raise FileExistsError("comparison destination already exists")
    features = load_r06_features(features_component, expected_manifest_sha256=features_digest)
    ranker = load_residual_ranker(ranker_component, expected_manifest_sha256=ranker_digest)
    engines = {backend: load_r06_retrieval(retrieval_component, features,
                expected_manifest_sha256=retrieval_digest, content_backend=backend) for backend in ("stdlib", "numpy")}
    samples, references = _validation(features_component, features_digest), _validation(ranker_component, ranker_digest)
    if len(samples) != len(references):
        raise ValueError("feature and ranker reference counts differ")
    # All approved component loads/replays happen before the timed calls.
    output.mkdir(parents=True, exist_ok=False)
    report, report_owned = output / "verification.json", False
    try:
        source = output / "source"
        hashes = {}
        for relative in SOURCE_FILES:
            raw = (project / relative).read_bytes()
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            hashes[relative] = hashlib.sha256(raw).hexdigest()
        code = {"base_commit": subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip(),
                "working_tree_dirty": bool(subprocess.check_output(["git", "-C", str(project), "status", "--porcelain"], text=True).strip()),
                "source_sha256": hashes}
        measurements, rows, maximum = [], [], [0., 0., 0.]
        for row, (sample, reference) in enumerate(zip(samples, references, strict=True)):
            effective = math.sqrt(sum(v*v for v in sample["expected_context"])) > 1e-8
            for repeat in range(rounds):
                order = ("stdlib", "numpy", "numpy", "stdlib") if (row + repeat) % 2 == 0 else ("numpy", "stdlib", "stdlib", "numpy")
                for backend in order:
                    tick = time.perf_counter()
                    result = engines[backend].retrieve(sample["history"], sample["seen"], sample["timestamp_ms"])
                    seconds = time.perf_counter() - tick
                    if not math.isfinite(seconds) or seconds <= 0:
                        raise ValueError("comparison clock did not advance")
                    # Correctness checking is outside the timed retrieval scope.
                    errors = _verify(features, ranker, result, sample, reference)
                    maximum = [max(a,b) for a,b in zip(maximum, errors, strict=True)]
                    measurements.append({"row": row, "round": repeat, "backend": backend, "seconds": seconds,
                                         "effective_history": effective})
            medians = {backend: statistics.median(m["seconds"] for m in measurements if m["row"] == row
                       and m["backend"] == backend) for backend in engines}
            rows.append({"row": row, "effective_history": effective, "median_seconds": medians,
                         "speedup": medians["stdlib"] / medians["numpy"]})
            print(json.dumps({"checked_row": row, **rows[-1]}), flush=True)
        if any(row["effective_history"] for row in rows):
            active = {backend: statistics.median(m["seconds"] for m in measurements if m["effective_history"]
                      and m["backend"] == backend) for backend in engines}
            speedup = active["stdlib"] / active["numpy"]
        else:
            active, speedup = None, None
        if any(hashlib.sha256((project / relative).read_bytes()).hexdigest() != digest
               for relative, digest in hashes.items()):
            raise ValueError("source changed during comparison")
        import numpy as np
        from evorec.infrastructure._content_numpy import BLOCK_ITEMS
        result = {"status": "passed", "component_only": True, "activated": False,
                  "scope": "same-process warm retrieval only; not load time, full recommendation latency or SLA",
                  "order": "alternating ABBA/BAAB", "rounds": rounds, "rows": rows, "measurements": measurements,
                  "effective_history_median_seconds": active, "effective_history_speedup": speedup,
                  "all_full_provider_orders_exact": True, "top20_exact": True,
                  "max_context_error": maximum[0], "max_scalar_error": maximum[1], "max_ranker_score_error": maximum[2],
                  "features_manifest_sha256": features.manifest_sha256, "retrieval_manifest_sha256": retrieval_digest,
                  "ranker_manifest_sha256": ranker.manifest_sha256, "item_count": len(features.item_ids),
                  "dimension": features.dimension, "test_queries_evaluated": False, "retrained": False,
                  "block_items": BLOCK_ITEMS, "max_product_matrix_bytes": BLOCK_ITEMS * features.dimension * 4,
                  "content_arithmetic": "f32-product-sequential-f32-sum-v1",
                  "python_version": sys.version.split()[0], "numpy_version": np.__version__,
                  "platform": platform.platform(), "source": code}
        with report.open("xb") as stream:
            report_owned = True
            stream.write(json.dumps(result, sort_keys=True, allow_nan=False).encode("utf-8"))
        return result
    except BaseException:
        if report_owned:
            report.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--features-component", type=Path, required=True)
    parser.add_argument("--expected-features-manifest-sha256", required=True)
    parser.add_argument("--retrieval-component", type=Path, required=True)
    parser.add_argument("--expected-retrieval-manifest-sha256", required=True)
    parser.add_argument("--ranker-component", type=Path, required=True)
    parser.add_argument("--expected-ranker-manifest-sha256", required=True)
    parser.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args(argv)
    result = benchmark(args.output, args.features_component, args.expected_features_manifest_sha256,
                       args.retrieval_component, args.expected_retrieval_manifest_sha256,
                       args.ranker_component, args.expected_ranker_manifest_sha256, rounds=args.rounds)
    print(json.dumps({"status": result["status"], "effective_history_median_seconds": result["effective_history_median_seconds"],
                      "effective_history_speedup": result["effective_history_speedup"], "activated": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
