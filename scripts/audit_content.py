"""Independently audit the static-content experiment and its stored artifacts."""
import argparse
import csv
import gzip
import hashlib
import json
import math
import statistics
from bisect import bisect_left
from collections import defaultdict, deque
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def audit(run, output):
    series = json.loads((run / "series.json").read_text())
    assert series["status"] == "completed"
    config = series["configuration"]
    provenance = series["data_provenance"]
    assert sha(config["dataset_path"]) == provenance["sample_sha256"]
    assert sha(provenance["catalog_path"]) == provenance["catalog_sha256"]
    for name, expected in series["code"]["source_sha256"].items():
        assert sha(run / "source" / name) == expected
    assert json.loads((run / "configuration.json").read_text()) == config
    with Path(config["dataset_path"]).open(newline="", encoding="utf-8") as stream:
        rows = [(int(r["timestamp"]), r["user_id"], r["parent_asin"], float(r["rating"]))
                for r in csv.DictReader(stream)]
    rows.sort()
    assert len({(user, item) for _, user, item, _ in rows}) == len(rows)
    catalog = json.loads(Path(provenance["catalog_path"]).read_text())
    catalog_times = sorted(catalog.values())
    train_items = {item for timestamp, _, item, _ in rows if timestamp < config["train_end_ms"]}
    histories, seen = defaultdict(lambda: deque(maxlen=config["history_limit"])), defaultdict(set)
    expected_queries = []
    for timestamp, iterator in groupby(rows, key=lambda row: row[0]):
        batch = list(iterator)
        for _, user, target, rating in batch:
            if timestamp >= config["validation_end_ms"] and rating >= config["positive_rating_min"]:
                query_id = hashlib.sha256(f"{user}\0{target}\0{timestamp}".encode()).hexdigest()[:24]
                expected_queries.append({
                    "query_id": query_id, "timestamp_ms": timestamp, "target": target,
                    "history": list(histories[user]), "target_available": catalog[target] < timestamp,
                    "target_model_cold": target not in train_items, "seen": frozenset(seen[user]),
                })
        for _, user, item, rating in batch:
            seen[user].add(item)
            if rating >= config["positive_rating_min"]:
                histories[user].append(item)
    assert len(expected_queries) == series["test_queries"]
    assert len({q["query_id"] for q in expected_queries}) == len(expected_queries)
    metric_keys = ("ndcg@10", "recall@20", "candidate_recall@200")
    checked_rows, checked_candidates, summaries = 0, 0, []
    for result in series["test_results"]:
        cold_candidate_count = 0
        trace = result["trace"]
        assert sha(trace["path_from_project_root"]) == trace["sha256"]
        totals = {}
        for group in result["metrics"]["cohorts"]:
            totals[group] = {"n": 0, "sum": [0.0, 0.0, 0.0], "items": set(), "catalog": 0}
        with gzip.open(trace["path_from_project_root"], "rt", encoding="utf-8") as stream:
            for expected, line in zip(expected_queries, stream, strict=True):
                record = json.loads(line)
                for field in ("query_id", "timestamp_ms", "target", "history", "target_available", "target_model_cold"):
                    assert record[field] == expected[field], (result["name"], field)
                candidates = record["recommendations"]
                assert len(candidates) <= config["candidate_k"]
                assert len(set(candidates)) == len(candidates)
                assert not expected["seen"].intersection(candidates)
                assert all(catalog[item] < expected["timestamp_ms"] for item in candidates)
                rank = next((i for i, item in enumerate(candidates, 1) if item == expected["target"]), math.inf)
                values = [1 / math.log2(rank + 1) if rank <= 10 else 0.0, float(rank <= 20), float(rank <= 200)]
                for key, value in zip(metric_keys, values, strict=True):
                    assert math.isclose(record["metrics"][key], value, abs_tol=1e-12)
                groups = ["all_positive_events", "history_present" if expected["history"] else "cold_user"]
                if expected["target_available"]:
                    groups.append("available_target")
                    if expected["target_model_cold"]:
                        groups.append("model_cold_available")
                for group in groups:
                    acc = totals[group]
                    acc["n"] += 1
                    acc["sum"] = [old + value for old, value in zip(acc["sum"], values, strict=True)]
                    acc["items"].update(candidates[:10])
                    acc["catalog"] = max(acc["catalog"], bisect_left(catalog_times, expected["timestamp_ms"]))
                checked_rows += 1
                checked_candidates += len(candidates)
                cold_candidate_count += sum(item not in train_items for item in candidates)
        for group, acc in totals.items():
            measured = result["metrics"]["cohorts"][group]
            assert acc["n"] == measured["n"]
            for key, total in zip(metric_keys, acc["sum"], strict=True):
                if acc["n"]:
                    assert math.isclose(total / acc["n"], measured[key], abs_tol=1e-12)
                else:
                    assert measured[key] is None
            assert acc["catalog"] == measured["coverage_catalog_items"]
            assert len(acc["items"]) == measured["unique_recommended_items@10"]
            if acc["catalog"]:
                assert math.isclose(len(acc["items"]) / acc["catalog"], measured["catalog_coverage@10"], abs_tol=1e-12)
        summaries.append({"name": result["name"], "rows": totals["all_positive_events"]["n"], "sha256": trace["sha256"], "model_cold_candidates": cold_candidate_count})
    first_seed = config["seeds"][0]
    selected = max((t for t in series["trials"] if t["seed"] == first_seed),
                   key=lambda t: t["best_validation"]["cohorts"]["all_positive_events"]["ndcg@10"])
    assert selected["setting"] == series["selected_setting"]
    assert series["selection_finished_at"] < series["test_opened_at"] < series["finished_at"]
    for trial in series["trials"]:
        best_epoch = max(trial["history"], key=lambda r: r["validation_ndcg@10"])
        assert best_epoch["epoch"] == trial["best_epoch"]
        assert math.isclose(best_epoch["validation_ndcg@10"], trial["best_validation"]["cohorts"]["all_positive_events"]["ndcg@10"], abs_tol=1e-12)
        assert sha(trial["checkpoint"]["path_from_project_root"]) == trial["checkpoint"]["sha256"]
    import joblib
    import numpy as np
    import torch
    from collections import Counter
    metadata_path = Path(config["metadata_path"])
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert sha(metadata_path) == series["metadata_provenance"]["metadata_sha256"]
    feature_dir = run / "content-encoder"
    feature_manifest = series["content_encoder"]
    assert feature_manifest["protocol_id"] == series["protocol_id"]
    for name, expected in feature_manifest["files"].items():
        assert sha(feature_dir / name) == expected
    items = json.loads((feature_dir / "items.json").read_text())
    vectors = np.load(feature_dir / "vectors.npy")
    digest = hashlib.sha256(vectors.tobytes(order="C"))
    digest.update(json.dumps(items).encode())
    fingerprint = digest.hexdigest()
    assert np.isfinite(vectors).all()
    fit_items = sorted({item for timestamp, _, item, rating in rows
                        if timestamp < config["train_end_ms"] and rating >= config["positive_rating_min"]
                        and metadata.get(item, "").strip()})
    assert len(fit_items) == feature_manifest["fit_document_count"]
    assert hashlib.sha256("\n".join(fit_items).encode()).hexdigest() == feature_manifest["fit_item_set_sha256"]
    encoder = joblib.load(feature_dir / "encoder.joblib")
    analyzer = encoder["vectorizer"].build_analyzer()
    document_frequency = Counter()
    for item in fit_items:
        document_frequency.update(set(analyzer(metadata[item])))
    vocabulary = encoder["vectorizer"].vocabulary_
    expected_idf = np.zeros(len(vocabulary))
    for term, index in vocabulary.items():
        assert document_frequency[term] >= config["content"]["min_df"]
        expected_idf[index] = np.log((1+len(fit_items))/(1+document_frequency[term])) + 1
    np.testing.assert_allclose(encoder["vectorizer"].idf_, expected_idf, atol=2e-6)
    for trial in series["trials"]:
        checkpoint = torch.load(trial["checkpoint"]["path_from_project_root"], weights_only=True, map_location="cpu")
        assert checkpoint["protocol_id"] == series["protocol_id"]
        assert checkpoint["feature_fingerprint"] == fingerprint
        assert checkpoint["epoch"] == trial["best_epoch"]
    for family, summary in series["seed_summaries"].items():
        neural = [row for row in series["test_results"] if row.get("family") == family]
        assert [row["seed"] for row in neural] == config["seeds"]
        assert all(row["checkpoint_reload_verified"] for row in neural)
        for key, saved in summary.items():
            cohort = "model_cold_available" if key.startswith("cold_") else "all_positive_events"
            metric = key.removeprefix("cold_")
            values = [row["metrics"]["cohorts"][cohort][metric] for row in neural]
            assert math.isclose(statistics.mean(values), saved["mean"], abs_tol=1e-12)
            assert math.isclose(statistics.stdev(values), saved["sample_std"], abs_tol=1e-12)
    fusion_options = [r for r in series["baselines"] if r["name"].startswith("Tower-RRF-")]
    fusion_best = max(fusion_options, key=lambda r: r["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"])
    assert f"a{series['selected_fusion_alpha']}-" in fusion_best["name"]
    method_options = [(r["name"], r["metrics"]) for r in series["baselines"]]
    method_options.append((selected["name"], selected["best_validation"]))
    assert max(method_options, key=lambda r: r[1]["cohorts"]["all_positive_events"]["ndcg@10"])[0] == series["selected_validation_method"]
    report = {
        "status": "passed", "checked_at": datetime.now(timezone.utc).isoformat(),
        "protocol_id": series["protocol_id"], "series_sha256": sha(run / "series.json"),
        "audit_script_sha256": sha(__file__), "test_queries": len(expected_queries),
        "trace_records_checked": checked_rows, "candidate_eligibility_checks": checked_candidates,
        "checks": ["sample/catalog/source/config fingerprints", "independent strict-time query reconstruction",
                   "identical query order across methods", "no seen/unavailable/duplicate candidates",
                   "independent per-request and all five cohort metrics/coverage",
                   "validation-only selection and best epochs", "checkpoint hashes and recorded reload equality",
                   "three-seed mean and sample standard deviation", "metadata and frozen encoder fingerprints",
                   "independent training-only TF-IDF document frequencies", "checkpoint binding to exact content vectors",
                   "validation-only fusion and overall method selection"],
        "traces": summaries,
        "limits": ["static metadata timestamps unavailable; content access is an auxiliary assumption",
                   "does not establish statistical significance or online performance",
                   "checkpoint inference equality was measured by the training run; this audit verifies its evidence and hashes"],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("status", "test_queries", "trace_records_checked", "candidate_eligibility_checks")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit(args.run, args.output)
