"""Independent R04 traces, frozen-training cold labels and reservation audit."""
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

import numpy as np


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def events(path):
    with Path(path).open(newline="", encoding="utf-8") as stream:
        return sorted((int(r["timestamp"]), r["user_id"], r["parent_asin"], float(r["rating"]))
                      for r in csv.DictReader(stream))


def traces(row, key="trace"):
    return gzip.open(row[key]["path_from_project_root"], "rt", encoding="utf-8")


def audit(run, output):
    series = read(run/"series.json")
    assert series["status"] == "completed"
    c, provenance = series["configuration"], series["data_provenance"]
    frozen = read(Path(c["frozen_run"])/"series.json")
    assert sha(Path(c["frozen_run"])/"series.json") == series["frozen"]["series_sha256"]
    assert read(run/"configuration.json") == c
    assert sha(c["dataset_path"]) == provenance["sample_sha256"]
    assert sha(provenance["catalog_path"]) == provenance["catalog_sha256"]
    assert sha(c["metadata_path"]) == series["metadata_provenance"]["metadata_sha256"]
    for name, expected in series["code"]["source_sha256"].items():
        assert sha(run/"source"/name) == expected
    for name, expected in series["frozen"]["inference_source_sha256"].items():
        assert sha(run/"source"/name) == expected == frozen["code"]["source_sha256"][name]
    train_path = frozen["configuration"]["dataset_path"]
    assert sha(train_path) == frozen["data_provenance"]["sample_sha256"]
    rows, model_rows = events(c["dataset_path"]), events(train_path)
    users = {r[1] for r in rows}
    assert not users & {r[1] for r in model_rows}
    for earlier in ("datasets/video_games_r01.csv", "datasets/video_games_r02.csv"):
        assert not users & {r[1] for r in events(earlier)}
    assert len({(u, i) for _, u, i, _ in rows}) == len(rows) == provenance["rows"]
    # Frozen R03 train items, including negative ratings, define model-cold.
    train_items = {i for t, _, i, _ in model_rows if t < c["train_end_ms"]}
    catalog = read(provenance["catalog_path"])
    catalog_times = sorted(catalog.values())
    histories, seen = defaultdict(lambda: deque(maxlen=c["history_limit"])), defaultdict(set)
    expected = {"validation": [], "test": []}
    for timestamp, group in groupby(rows, key=lambda r: r[0]):
        batch = list(group)
        for _, user, item, rating in batch:
            if timestamp >= c["train_end_ms"] and rating >= c["positive_rating_min"]:
                split = "validation" if timestamp < c["validation_end_ms"] else "test"
                expected[split].append({
                    "query_id": hashlib.sha256(f"{user}\0{item}\0{timestamp}".encode()).hexdigest()[:24],
                    "timestamp_ms": timestamp, "target": item, "history": list(histories[user]),
                    "seen": frozenset(seen[user]), "target_available": catalog[item] < timestamp,
                    "target_model_cold": item not in train_items,
                })
        for _, user, item, rating in batch:
            seen[user].add(item)
            if rating >= c["positive_rating_min"]:
                histories[user].append(item)
    feature_dir = Path(c["frozen_run"])/"content-encoder"
    for name, expected_sha in series["frozen"]["encoder"]["files"].items():
        assert sha(feature_dir/name) == expected_sha
    vectors = np.load(feature_dir/"vectors.npy")
    items = read(feature_dir/"items.json")
    mapping = {item: i for i, item in enumerate(items)}
    digest = hashlib.sha256(vectors.tobytes(order="C"))
    digest.update(json.dumps(items).encode())
    assert digest.hexdigest() == series["frozen"]["feature_fingerprint"]
    import torch
    for trial in series["frozen"]["trials"]:
        assert sha(trial["checkpoint"]["path_from_project_root"]) == trial["checkpoint"]["sha256"]
        cp = torch.load(trial["checkpoint"]["path_from_project_root"], weights_only=True, map_location="cpu")
        assert cp["protocol_id"] == frozen["protocol_id"]
        assert cp["feature_fingerprint"] == digest.hexdigest()
    checked_rows, checked_candidates, gate_checks = 0, 0, 0
    metric_keys = ("ndcg@10", "recall@20", "candidate_recall@200")
    summaries = []
    for split, queries in expected.items():
        results = series[split+"_results"]
        lookup = {r["name"]: r for r in results}
        signals = []
        for q in queries:
            represented = [(len(q["history"])-1-j, vectors[mapping[item]].astype("float64"))
                           for j, item in enumerate(q["history"]) if item in mapping
                           and np.linalg.norm(vectors[mapping[item]]) > 1e-8]
            weights = np.array([c["content"]["history_decay"]**distance for distance, _ in represented])
            coherence = min(1., float(np.linalg.norm(np.average([v for _, v in represented], axis=0, weights=weights)))) if represented else 0.
            signals.append((len(represented), coherence))
        for result in results:
            assert sha(result["trace"]["path_from_project_root"]) == result["trace"]["sha256"]
            totals = {group: {"n": 0, "sum": [0., 0., 0.], "items": set(), "catalog": 0}
                      for group in result["metrics"]["cohorts"]}
            is_policy = "policy" in result
            if is_policy:
                assert sha(result["gate_trace"]["path_from_project_root"]) == result["gate_trace"]["sha256"]
                gate_stream = traces(result, "gate_trace")
                base_stream = traces(lookup[f"fixed-s{result['seed']}"])
                raw_stream = traces(lookup["Content-SVD"])
                stats = {"requests": len(queries), "gate_passed": 0, "ranking_changed": 0, "new_cold_promoted": 0}
            with traces(result) as stream:
                for index, (q, line) in enumerate(zip(queries, stream, strict=True)):
                    entry = json.loads(line)
                    for field in ("query_id", "timestamp_ms", "target", "history", "target_available", "target_model_cold"):
                        assert entry[field] == q[field], (split, result["name"], field)
                    recs = entry["recommendations"]
                    assert len(recs) <= 200 and len(set(recs)) == len(recs)
                    assert not q["seen"].intersection(recs)
                    assert all(catalog[item] < q["timestamp_ms"] for item in recs)
                    if is_policy:
                        gate = json.loads(next(gate_stream))
                        base = json.loads(next(base_stream))["recommendations"]
                        raw = json.loads(next(raw_stream))["recommendations"]
                        count, coherence = signals[index]
                        assert gate["query_id"] == q["query_id"] and gate["represented_history"] == count
                        assert math.isclose(gate["coherence"], coherence, abs_tol=2e-7)
                        p = result["policy"]
                        active = not p["gated"] or count >= c["gate"]["min_history"] and coherence >= c["gate"]["min_coherence"]
                        assert gate["gate_passed"] == active
                        old_cold = [item for item in base[:20] if item not in train_items]
                        promoted = 0
                        intended = list(base)
                        if active and len(old_cold) < p["quota"]:
                            additions = [item for item in raw if item not in train_items and item not in old_cold][:p["quota"]-len(old_cold)]
                            promoted = len(additions)
                            if additions:
                                chosen = old_cold + additions
                                pool = list(dict.fromkeys(base+raw))
                                pool = [item for item in pool if item not in chosen][:200]
                                for position, item in zip(c["gate"]["positions"][-len(chosen):], chosen, strict=True):
                                    pool.insert(min(position-1, len(pool)), item)
                                intended = pool[:200]
                                assert set(old_cold).issubset(recs[:20])
                        assert recs == intended
                        assert gate["new_cold_promoted"] == promoted
                        assert gate["ranking_changed"] == (recs != base)
                        for key in ("gate_passed", "ranking_changed", "new_cold_promoted"):
                            stats[key] += gate[key]
                        gate_checks += 1
                    rank = recs.index(q["target"])+1 if q["target"] in recs else math.inf
                    values = [1/math.log2(rank+1) if rank <= 10 else 0., float(rank <= 20), float(rank <= 200)]
                    for key, value in zip(metric_keys, values, strict=True):
                        assert math.isclose(entry["metrics"][key], value, abs_tol=1e-12)
                    groups = ["all_positive_events", "history_present" if q["history"] else "cold_user"]
                    if q["target_available"]:
                        groups += ["available_target"]
                        if q["target_model_cold"]:
                            groups += ["model_cold_available"]
                    for group in groups:
                        acc = totals[group]
                        acc["n"] += 1
                        acc["sum"] = [a+b for a,b in zip(acc["sum"], values, strict=True)]
                        acc["items"].update(recs[:10])
                        acc["catalog"] = max(acc["catalog"], bisect_left(catalog_times, q["timestamp_ms"]))
                    checked_rows += 1
                    checked_candidates += len(recs)
            if is_policy:
                assert not gate_stream.read() and not base_stream.read() and not raw_stream.read()
                for handle in (gate_stream, base_stream, raw_stream):
                    handle.close()
                assert stats == result["gate_statistics"]
            for group, acc in totals.items():
                m = result["metrics"]["cohorts"][group]
                assert m["n"] == acc["n"]
                for key, total in zip(metric_keys, acc["sum"], strict=True):
                    assert m[key] is None if not acc["n"] else math.isclose(m[key], total/acc["n"], abs_tol=1e-12)
                assert m["coverage_catalog_items"] == acc["catalog"]
                assert m["unique_recommended_items@10"] == len(acc["items"])
                if acc["catalog"]:
                    assert math.isclose(m["catalog_coverage@10"], len(acc["items"])/acc["catalog"], abs_tol=1e-12)
            summaries.append({"split": split, "name": result["name"], "rows": len(queries)})
    choices = [r for r in series["validation_results"] if "policy" in r]
    assert [r["policy"] for r in choices] == c["policies"]
    assert all(r["seed"] == c["selection"]["seed"] for r in choices)
    floor = choices[0]["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"]*c["selection"]["ndcg_min_ratio"]
    eligible = [r for r in choices if r["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"] >= floor]
    chosen = max(eligible, key=lambda r: (r["metrics"]["cohorts"]["model_cold_available"]["recall@20"],
                                         r["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"], -r["policy"]["quota"]))
    assert chosen["policy"] == series["selected_policy"] and floor == series["validation_ndcg_floor"]
    assert series["selection_finished_at"] < series["test_opened_at"] < series["finished_at"]
    for name, summary in series["seed_summaries"].items():
        members = [r for r in series["test_results"] if r.get("policy", {}).get("name") == name]
        assert [r["seed"] for r in members] == c["seeds"]
        for key, saved in summary.items():
            group = "model_cold_available" if key.startswith("cold_") else "all_positive_events"
            values = [r["metrics"]["cohorts"][group][key.removeprefix("cold_")] for r in members]
            assert math.isclose(statistics.mean(values), saved["mean"], abs_tol=1e-12)
            assert math.isclose(statistics.stdev(values), saved["sample_std"], abs_tol=1e-12)
    report = {
        "status": "passed", "checked_at": datetime.now(timezone.utc).isoformat(),
        "protocol_id": series["protocol_id"], "series_sha256": sha(run/"series.json"),
        "audit_script_sha256": sha(__file__), "trace_records_checked": checked_rows,
        "candidate_eligibility_checks": checked_candidates, "gate_records_reconstructed": gate_checks,
        "validation_queries": len(expected["validation"]), "test_queries": len(expected["test"]),
        "checks": ["strict-time histories from R04; cold membership from frozen R03",
                   "R01/R02/R03 user disjointness", "five cohort metrics and coverage",
                   "source/config/sample/metadata/encoder/checkpoint fingerprints",
                   "independent history signals and exact policy reconstruction",
                   "validation-only quality floor and tie-break", "three-seed mean/sample SD"],
        "traces": summaries,
        "limits": ["static metadata assumption", "no statistical significance test",
                   "rule gate only; retrieval costs are not avoided", "no online performance claim"],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({k:report[k] for k in ("status", "trace_records_checked", "candidate_eligibility_checks", "gate_records_reconstructed")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit(args.run, args.output)
