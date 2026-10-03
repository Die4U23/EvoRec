"""Export frozen R06 vectors/statistics and replay label-free validation pools.

Uses previously frozen provider outputs, not a new retrieval experiment. Reading
the full rating sample reconstructs validation history/seen; no test queries or
test metrics are evaluated. Raw request/item data remains in ignored artifacts.
"""

import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import sklearn

from evorec.infrastructure.r06_features import load_r06_features
from evorec.infrastructure.residual_ranker import load_residual_ranker, SCORE_TOLERANCE
from evorec.research.content import ContentFeatures
from evorec.research.data import load_events
from evorec.research.export_ranker import _checked, _json_bytes, _read, _record, _sha, _snapshot_code
from evorec.research.protocol import Protocol
from evorec.research.ranker import SCALAR_NAMES
from evorec.research.training_baselines import RecentPopular


def write_features(output, features, metadata, provenance, samples):
    output = Path(output)
    vectors = features.vectors.astype("<f4").tobytes(order="C")
    items, validation = _json_bytes(metadata), _json_bytes(samples)
    manifest = {
        "schema_version": 1, "kind": "r06-pool-features-v1", "dtype": "float32-le",
        "dimension": features.vectors.shape[1], "item_count": len(features.items),
        "history_decay": .8, "history_limit": 50, "rrf_constant": 60,
        "scalar_names": list(SCALAR_NAMES), "provenance": provenance,
        "items": _record("items.json", items), "vectors": _record("vectors.f32", vectors),
        "validation": _record("validation.json", validation),
    }
    output.mkdir(parents=True, exist_ok=False)
    manifest_path = output / "manifest.json"
    try:
        for name, raw in (("items.json", items), ("vectors.f32", vectors), ("validation.json", validation)):
            (output / name).write_bytes(raw)
        manifest_path.write_bytes(_json_bytes(manifest))
        return load_r06_features(output, expected_feature_fingerprint=features.fingerprint)
    except BaseException:
        manifest_path.unlink(missing_ok=True)
        raise


def _verify_ranker(runtime, samples, ranker_component, expected_manifest_sha256):
    ranker = load_residual_ranker(ranker_component, expected_manifest_sha256=expected_manifest_sha256)
    reference_scores = _read(Path(ranker_component) / "validation.json")
    errors, rank_matches = [], []
    context_error, scalar_error = 0., 0.
    for sample, reference in zip(samples, reference_scores, strict=True):
        pool = runtime.build_pool(*(sample[key] for key in ("history", "seen", "timestamp_ms", "collaborative", "content")))
        scores = pool.score(ranker)  # Checks all shared source/protocol identities before inference.
        context_error = max(context_error, max(abs(a-b) for a,b in zip(pool.context, sample["expected_context"], strict=True)))
        scalar_error = max(scalar_error, max((abs(a-b) for actual, expected in zip(pool.scalars, sample["expected_scalars"], strict=True)
                                             for a,b in zip(actual, expected, strict=True)), default=0.))
        errors.extend(abs(a-b) for a,b in zip(scores, reference["expected_scores"], strict=True))
        rank_matches.append(sorted(range(len(scores)), key=lambda i: -scores[i])[:20] == reference["expected_top20"])
    if not errors or max(errors) > SCORE_TOLERANCE or not all(rank_matches):
        raise ValueError("rebuilt features changed frozen ranker scores or Top-20")
    return {"max_context_error": context_error, "max_scalar_error": scalar_error,
            "max_ranker_score_error": max(errors), "top20_exact": True}


def _finish_export(output, report, features, metadata, provenance, samples,
                   ranker_component, expected_ranker_manifest_sha256, details):
    # write_features owns only a newly created directory; an existing component
    # must never be invalidated by our failure cleanup.
    report = Path(report)
    if report.exists():
        raise FileExistsError("feature verification report already exists")
    runtime = write_features(output, features, metadata, provenance, samples)
    try:
        metrics = _verify_ranker(runtime, samples, ranker_component, expected_ranker_manifest_sha256)
        result = {
            **details, **metrics, "status": "passed", "component_only": True, "activated": False,
            "manifest_sha256": runtime.manifest_sha256, "item_count": len(runtime.item_ids),
            "dimension": runtime.dimension, "provenance": provenance,
            "candidate_counts": [len(sample["expected_items"]) for sample in samples],
            "test_queries_evaluated": False, "retrieval_recomputed": False, "new_text_encoded": False,
        }
        report.write_bytes(_json_bytes(result))
        return result
    except BaseException:
        (Path(output) / "manifest.json").unlink(missing_ok=True)
        report.unlink(missing_ok=True)
        raise


def export(output, ranker_component):
    project = Path.cwd().resolve()
    output = Path(output).resolve()
    if not output.is_relative_to(project / "artifacts") or output == project / "artifacts":
        raise ValueError("feature export must use a new artifacts subdirectory")
    snapshot, report = output.with_name(output.name + "-source"), output.with_name(output.name + "-verification.json")
    if any(path.exists() for path in (output, snapshot, report)):
        raise FileExistsError("feature export destination already exists")
    archive = project / "docs/experiments/archive/r06-multi-interest-20260917.json"
    series = _read(archive)
    config = series["configuration"]
    if (series["status"] != "completed" or series["selected_method"] != "A-frozen-s17"
            or config["content"]["history_decay"] != .8 or config["history_limit"] != 50
            or config["rrf_constant"] != 60 or config["retrieval_k"] != 200 or config["pool_k"] != 400):
        raise ValueError("not the fixed validation-selected R06 feature protocol")
    source = project / config["source_run"]
    original = _read(_checked(project, {"path_from_project_root": str(source / "series.json"),
                                       "sha256": config["source_series_sha256"]}))
    files = original["content_encoder"]["files"]
    encoder = source / "content-encoder"
    checked = {name: _checked(project, {"path_from_project_root": str(encoder / name), "sha256": files[name]})
               for name in ("items.json", "vectors.npy")}
    features = ContentFeatures(_read(checked["items.json"]), np.load(checked["vectors.npy"], allow_pickle=False))
    if features.fingerprint != series["feature_fingerprint"]:
        raise ValueError("frozen feature fingerprint changed")
    catalog = _read(_checked(project, {"path_from_project_root": series["data_provenance"]["catalog_path"],
                                      "sha256": series["data_provenance"]["catalog_sha256"]}))
    # These fixed source paths are inputs of the archived experiment, not exports
    # selected from metrics. The full sample hash is checked before reconstruction.
    training_path = _checked(project, {"path_from_project_root": "datasets/video_games_r03.csv",
                                       "sha256": series["model_training_provenance"]["sample_sha256"]})
    query_path = _checked(project, {"path_from_project_root": config["dataset_path"],
                                    "sha256": series["data_provenance"]["sample_sha256"]})
    events, _ = load_events(training_path)
    train = [event for event in events if event.timestamp_ms < config["train_end_ms"]]
    training_items = {event.item_id for event in train}  # Includes low-rating interactions.
    prior = RecentPopular(365).fit(train, config["train_end_ms"], config["positive_rating_min"])
    maximum = max(prior.counts.values())
    priors = {item: math.log1p(value) / math.log1p(maximum) for item, value in prior.counts.items()}
    metadata = [{"item_id": item, "first_seen_ms": catalog[item], "training_item": item in training_items,
                 "prior_strength": priors.get(item, 0.)} for item in features.items]
    query_protocol = Protocol(config)
    if (query_protocol.manifest["sample_sha256"] != _sha(query_path)
            or query_protocol.manifest["catalog_sha256"] != series["data_provenance"]["catalog_sha256"]):
        raise ValueError("validation history source changed")
    queries = query_protocol.queries("validation")
    pool_record = series["validation_candidates"]["caches"]["A"]
    with np.load(_checked(project, pool_record), allow_pickle=False) as cache:
        ids, contexts, scalars = cache["items"], cache["contexts"], cache["scalars"]
    if len(queries) != len(ids):
        raise ValueError("validation query count differs from frozen pool")
    represented = np.flatnonzero(np.linalg.norm(contexts, axis=1) > 1e-8)
    empty = np.flatnonzero(np.linalg.norm(contexts, axis=1) <= 1e-8)
    if len(represented) < 3 or not len(empty):
        raise ValueError("validation lacks represented and empty history references")
    rows = [int(represented[i]) for i in (0, len(represented) // 2, -1)] + [int(empty[0])]
    trace_record = series["validation_candidates"]["source_trace"]
    records = {}
    with gzip.open(_checked(project, trace_record), "rt", encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            if index in rows:
                records[index] = json.loads(line)
    samples = []
    for row in rows:
        query, trace = queries[row], records[row]
        if query.query_id != trace["query_id"] or query.timestamp_ms != trace["timestamp_ms"]:
            raise ValueError("validation trace and reconstructed request differ")
        valid = ids[row] > 0
        samples.append({
            "history": list(query.history), "seen": sorted(query.seen), "timestamp_ms": query.timestamp_ms,
            "collaborative": trace["providers"]["cf"], "content": trace["providers"]["centroid"],
            "expected_items": [features.items[int(i) - 1] for i in ids[row, valid]],
            "expected_context": contexts[row].tolist(), "expected_scalars": scalars[row, valid].tolist(),
        })
    provenance = {
        "feature_fingerprint": features.fingerprint, "selection_series_sha256": _sha(archive),
        "source_series_sha256": config["source_series_sha256"],
        "training_sample_sha256": series["model_training_provenance"]["sample_sha256"],
        "catalog_sha256": series["data_provenance"]["catalog_sha256"],
        "items_sha256": files["items.json"], "vectors_sha256": files["vectors.npy"],
        "validation_pool_sha256": pool_record["sha256"], "validation_sources_sha256": trace_record["sha256"],
        "selection_protocol_id": series["protocol_id"], "training_protocol_id": original["protocol_id"],
    }
    code = _snapshot_code(snapshot)
    for relative in ("src/evorec/infrastructure/r06_features.py", "src/evorec/research/export_features.py",
                     "src/evorec/research/data.py", "src/evorec/research/protocol.py",
                     "src/evorec/research/training_baselines.py"):
        raw = (project / relative).read_bytes()
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        code["source_sha256"][relative] = hashlib.sha256(raw).hexdigest()
    code["sklearn_version"] = sklearn.__version__
    approved_ranker = _read(project / "docs/validation/r06-ranker-component-20261003.json")
    return _finish_export(output, report, features, metadata, provenance, samples, ranker_component,
                          approved_ranker["manifest_sha256"], {"validation_rows": rows, "export_code": code})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ranker-component", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(export(args.output, args.ranker_component), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
