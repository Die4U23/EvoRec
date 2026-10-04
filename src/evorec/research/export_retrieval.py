"""Reconstruct frozen training-only CF statistics; replay archived R06 providers.

No optimization/refit of neural or text models, no test queries/metrics. The full
rating CSV is parsed before the strict training cutoff filter. Only four already
fixed validation requests are recomputed, without accessing their target labels.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import time

from evorec.infrastructure.r06_features import load_r06_features, MAX_JSON_BYTES, _json
from evorec.infrastructure.r06_retrieval import load_r06_retrieval, retrieval_protocol
from evorec.infrastructure.residual_ranker import _read as _component_read, _verified
from evorec.research.baselines import ItemCF
from evorec.research.data import load_events
from evorec.research.export_features import _verify_ranker
from evorec.research.export_ranker import _checked, _json_bytes, _read, _record, _sha, _snapshot_code
from evorec.research.training_baselines import RecentPopular


def write_retrieval(output, features, counts, neighbors, fit, samples):
    """Export exact float64 similarities and priors without overwriting evidence."""
    offsets, edges = [0], bytearray()
    for item in features.item_ids:
        for other, similarity in neighbors.get(item, ()):
            edges.extend(struct.pack("<Id", features._indices[other], similarity))
        offsets.append(len(edges) // 12)
    graph = struct.pack(f"<{len(offsets)}I", *offsets) + edges
    statistics = _json_bytes([counts.get(item, 0.) for item in features.item_ids])
    validation = _json_bytes(samples)
    provenance = {**features.provenance, "features_manifest_sha256": features.manifest_sha256,
                  "selected_method": "A-frozen-s17"}
    manifest = {"schema_version": 1, "kind": "r06-cf-centroid-v1", "graph_dtype": "csr-u32-f64-le",
                "item_count": len(features.item_ids), "edge_count": len(edges) // 12,
                "protocol": retrieval_protocol(), "fit": fit, "provenance": provenance,
                "statistics": _record("statistics.json", statistics), "neighbors": _record("neighbors.bin", graph),
                "validation": _record("validation.json", validation)}
    if set(neighbors) - set(features.item_ids) or set(counts) - set(features.item_ids):
        raise ValueError("training graph or prior contains an item outside the frozen snapshot")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    try:
        for name, raw in (("statistics.json", statistics), ("neighbors.bin", graph), ("validation.json", validation)):
            (output / name).write_bytes(raw)
        (output / "manifest.json").write_bytes(_json_bytes(manifest))
        return load_r06_retrieval(output, features)
    except BaseException:
        (output / "manifest.json").unlink(missing_ok=True)
        raise


def _finish_export(output, report, features, counts, neighbors, fit, samples, full_samples,
                   ranker_component, expected_ranker_manifest_sha256, details):
    report = Path(report)
    if report.exists():
        raise FileExistsError("retrieval report already exists")
    started = time.perf_counter()
    runtime = write_retrieval(output, features, counts, neighbors, fit, samples)
    report_owned = False
    try:
        # Replace the frozen trace inputs with providers actually retrieved now;
        # write_retrieval has already required exact full-provider equality.
        rebuilt, seconds = [], []
        for reference in full_samples:
            tick = time.perf_counter()
            result = runtime.retrieve(reference["history"], reference["seen"], reference["timestamp_ms"])
            seconds.append(time.perf_counter() - tick)
            rebuilt.append({**reference, "collaborative": list(result.collaborative), "content": list(result.content)})
        metrics = _verify_ranker(features, rebuilt, ranker_component, expected_ranker_manifest_sha256)
        result = {**details, **metrics, "status": "passed", "component_only": True, "activated": False,
                  "manifest_sha256": runtime.manifest_sha256, "provenance": dict(runtime.provenance),
                  "fit": fit, "item_count": len(features.item_ids), "edge_count": runtime.edge_count,
                  "validation_samples": len(samples), "cf_full_order_exact": True, "content_full_order_exact": True,
                  "candidate_counts": [len(s["expected_items"]) for s in rebuilt],
                  "provider_counts": [[len(s["collaborative"]), len(s["content"])] for s in rebuilt],
                  "reference_retrieval_seconds": seconds, "export_and_replay_seconds": time.perf_counter() - started,
                  "test_queries_evaluated": False, "text_or_neural_models_retrained": False,
                  "cf_statistics_reconstructed": True, "retrieval_recomputed": True}
        with report.open("xb") as stream:
            report_owned = True
            stream.write(_json_bytes(result))
        return result
    except BaseException:
        (Path(output) / "manifest.json").unlink(missing_ok=True)
        if report_owned:
            report.unlink(missing_ok=True)
        raise


def export(project, output, features_component, expected_features_manifest_sha256,
           ranker_component, expected_ranker_manifest_sha256):
    project, output = Path(project).resolve(), Path(output).resolve()
    if not output.is_relative_to(project / "artifacts") or output == project / "artifacts":
        raise ValueError("retrieval export must use a new artifacts subdirectory")
    snapshot, report = output.with_name(output.name + "-source"), output.with_name(output.name + "-verification.json")
    if any(path.exists() for path in (output, snapshot, report)):
        raise FileExistsError("retrieval export destination already exists")
    features = load_r06_features(features_component, expected_manifest_sha256=expected_features_manifest_sha256)
    archive = project / "docs/experiments/archive/r06-multi-interest-20260917.json"
    series = _read(archive)
    config = series["configuration"]
    source = project / config["source_run"]
    original = _read(_checked(project, {"path_from_project_root": str(source / "series.json"),
                                       "sha256": config["source_series_sha256"]}))
    if (series["status"] != "completed" or original["status"] != "completed"
            or series["selected_method"] != "A-frozen-s17" or config["stage"] != "R06-multi-interest"
            or config["retrieval_k"] != 200 or config["history_limit"] != 50
            or config["content"]["history_decay"] != .8 or config["positive_rating_min"] != 4
            or config["train_end_ms"] != original["configuration"]["train_end_ms"]
            or features.provenance["selection_series_sha256"] != _sha(archive)
            or features.provenance["source_series_sha256"] != config["source_series_sha256"]
            or features.provenance["training_protocol_id"] != original["protocol_id"]
            or features.provenance["training_sample_sha256"] != series["model_training_provenance"]["sample_sha256"]):
        raise ValueError("not the frozen validation-selected R06 retrieval protocol")
    training_path = _checked(project, {"path_from_project_root": "datasets/video_games_r03.csv",
                                       "sha256": features.provenance["training_sample_sha256"]})
    catalog = _read(_checked(project, {"path_from_project_root": series["data_provenance"]["catalog_path"],
                                      "sha256": features.provenance["catalog_sha256"]}))
    if set(catalog) != set(features.item_ids) or any(catalog[i] != m.first_seen_ms
            for i, m in zip(features.item_ids, features._metadata, strict=True)):
        raise ValueError("frozen retrieval availability differs")
    events, _ = load_events(training_path)
    train = [event for event in events if event.timestamp_ms < config["train_end_ms"]]
    # This is reconstruction of the archived deterministic baseline, not a new
    # fit/selection of trainable weights. Low-rating interactions are excluded.
    prior = RecentPopular(365).fit(train, config["train_end_ms"], 4)
    core = ItemCF(max_user_items=100, neighbors=100).fit(train, 4)
    feature_root = Path(features_component).resolve()
    raw = _component_read(feature_root, "manifest.json", MAX_JSON_BYTES)
    if hashlib.sha256(raw).hexdigest() != features.manifest_sha256:
        raise ValueError("feature manifest changed during export")
    full_samples = _json(_verified(feature_root, _json(raw)["validation"], "validation.json", MAX_JSON_BYTES))
    keys = ("history", "seen", "timestamp_ms", "collaborative", "content")
    samples = [{key: sample[key] for key in keys} for sample in full_samples]
    code = _snapshot_code(snapshot)
    for relative in ("src/evorec/infrastructure/r06_features.py", "src/evorec/infrastructure/r06_retrieval.py",
                     "src/evorec/research/export_retrieval.py", "src/evorec/research/export_features.py",
                     "src/evorec/research/baselines.py", "src/evorec/research/training_baselines.py",
                     "src/evorec/research/data.py", "scripts/load_r06_retrieval.py",
                     "tests/test_r06_retrieval_runtime.py", "tests/test_r06_retrieval_export.py"):
        raw = (Path(__file__).resolve().parents[3] / relative).read_bytes()
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        code["source_sha256"][relative] = hashlib.sha256(raw).hexdigest()
    fit = {"end_ms": config["train_end_ms"], "training_rows": len(train), "positive_rating_min": 4}
    return _finish_export(output, report, features, prior.counts, core.neighbors, fit, samples, full_samples,
                          ranker_component, expected_ranker_manifest_sha256, {"export_code": code})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--features-component", type=Path, required=True)
    parser.add_argument("--expected-features-manifest-sha256", required=True)
    parser.add_argument("--ranker-component", type=Path, required=True)
    parser.add_argument("--expected-ranker-manifest-sha256", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(export(args.project_root, args.output, args.features_component,
                            args.expected_features_manifest_sha256, args.ranker_component,
                            args.expected_ranker_manifest_sha256), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
