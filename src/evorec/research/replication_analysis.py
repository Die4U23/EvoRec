"""Audit saved rankings, then compute the registered paired user intervals."""
import argparse
import gzip
import hashlib
import json
import math
import statistics
from pathlib import Path

import numpy as np

from evorec.research.baselines import Ranking
from evorec.research.protocol import summarize
from evorec.research.ranker_data import load_protocols, read
from evorec.research.replicate_ranker import checked_file
from evorec.research.replication_report import render
from evorec.research.runner import file_sha
from evorec.research.uncertainty import clustered_mean_interval


def trace_values(record, queries, protocol, expected, pool_items, feature_items):
    """Recompute ranks and legality from recommendations, ignoring saved metrics."""
    path = checked_file(record)
    values = []
    rankings = []
    signature = hashlib.sha256()
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for index, query in enumerate(queries):
            line = stream.readline()
            if not line:
                raise ValueError("trace is missing query rows")
            row = json.loads(line)
            expected_identity = {
                "query_id": query.query_id, "timestamp_ms": query.timestamp_ms,
                "target": query.target, "history": list(query.history),
                "target_available": query.target_available,
                "target_model_cold": query.target_model_cold,
            }
            if any(row[key] != value for key, value in expected_identity.items()):
                raise ValueError("trace query identity or labels differ")
            items = row["recommendations"]
            if len(items) > 200 or len(set(items)) != len(items):
                raise ValueError("invalid ranking size or duplicate")
            allowed = {feature_items[int(i) - 1] for i in pool_items[index] if i > 0}
            if any(item not in allowed or item in query.seen
                   or protocol.catalog.get(item, query.timestamp_ms) >= query.timestamp_ms
                   for item in items):
                raise ValueError("ranking violates original pool or time constraints")
            rank = items.index(query.target) + 1 if query.target in items else None
            ndcg = 1 / math.log2(rank + 1) if rank is not None and rank <= 10 else 0.0
            recall = float(rank is not None and rank <= 20)
            metrics = {"ndcg@10": ndcg, "recall@20": recall,
                       "candidate_recall@200": float(rank is not None and rank <= 200)}
            if metrics != row["metrics"]:
                raise ValueError("saved per-query metric differs from actual rank")
            values.append((ndcg, recall))
            rankings.append(Ranking(tuple(items)))
            signature.update(json.dumps([query.query_id, items], separators=(",", ":")).encode())
            signature.update(b"\n")
        if stream.readline():
            raise ValueError("trace contains extra rows")
    recomputed = summarize(protocol, queries, rankings)
    if recomputed["target_unavailable_events"] != expected["target_unavailable_events"]:
        raise ValueError("unavailable target count differs")
    # Provider attribution is absent from saved traces. Do not invent CF attribution.
    omitted = {"mean_personalized_candidates", "mean_popularity_fill"}
    for cohort, actual in recomputed["cohorts"].items():
        for key, value in actual.items():
            if key not in omitted and value != expected["cohorts"][cohort][key]:
                raise ValueError(f"recomputed cohort metric differs: {cohort}/{key}")
    return np.asarray(values), {
        "path": path.as_posix(), "sha256": file_sha(path), "queries": len(values),
        "candidate_checks": sum(len(r.items) for r in rankings),
        "ranking_signature": signature.hexdigest(),
        "unreconstructed_fields": sorted(omitted),
    }


def query_users(events, queries, config):
    """Recover private grouping IDs from the exact source query-ID definition."""
    mapping = {}
    for event in events:
        if (event.timestamp_ms >= config["validation_end_ms"]
                and event.rating >= config["positive_rating_min"]):
            raw = f"{event.user_id}\0{event.item_id}\0{event.timestamp_ms}"
            key = hashlib.sha256(raw.encode()).hexdigest()[:24]
            if key in mapping:
                raise ValueError("duplicate or colliding source query ID")
            mapping[key] = event.user_id
    if set(mapping) != {q.query_id for q in queries} or len(mapping) != len(queries):
        raise ValueError("source user mapping is not one-to-one with test queries")
    return np.asarray([mapping[q.query_id] for q in queries])


def analyze(run):
    series = read(run / "series.json")
    if series["status"] != "completed":
        raise ValueError("training must finish before analysis")
    config = series["configuration"]
    source = Path(config["source_run"])
    if file_sha(source / "series.json") != config["source_series_sha256"]:
        raise ValueError("source R05 changed")
    original = read(source / "series.json")
    _, protocol, _ = load_protocols(series["source_configuration"])
    queries = protocol.queries("test", test_authorized=True)
    users = query_users(protocol.events, queries, series["source_configuration"])
    feature_items = read(source / "content-encoder/items.json")
    with np.load(checked_file(original["test_cache"]), allow_pickle=False) as cached:
        test_pool = cached["items"]
    values, trace_audits = {}, []
    lookup = {row["name"]: row for row in original["test_results"]}
    for name in config["bootstrap"]["baselines"]:
        row = lookup[name]
        values[name], audit = trace_values(row["trace"], queries, protocol, row["metrics"],
                                           test_pool, feature_items)
        trace_audits.append(audit)
    for row in series["test_results"]:
        values[row["name"]], audit = trace_values(row["trace"], queries, protocol, row["metrics"],
                                                   test_pool, feature_items)
        trace_audits.append(audit)
    replay = lookup["ColdListMLP-s17"]
    _, replay_audit = trace_values(replay["trace"], queries, protocol, replay["metrics"],
                                   test_pool, feature_items)
    new_replay = next(a for a in trace_audits if a["path"].endswith("test-ColdListMLP-s17.jsonl.gz"))
    if new_replay["ranking_signature"] != replay_audit["ranking_signature"]:
        raise ValueError("seed 17 test rankings are not identical to the original")
    trace_audits.append(replay_audit)
    validation = protocol.queries("validation")
    with np.load(checked_file(original["validation_cache"]), allow_pickle=False) as cached:
        validation_pool = cached["items"]
    for trial in series["trials"]:
        _, audit = trace_values(trial["validation_trace"], validation, protocol,
                                 trial["best_validation"], validation_pool, feature_items)
        trace_audits.append(audit)
    names = [f"ColdListMLP-s{seed}" for seed in config["seeds"]]
    values["Fixed-seed metric mean"] = np.stack([values[name] for name in names]).mean(axis=0)
    masks = {
        "all_positive_events": np.ones(len(queries), dtype=bool),
        "history_present": np.asarray([bool(q.history) for q in queries]),
        "model_cold_available": np.asarray([q.target_available and q.target_model_cold for q in queries]),
    }
    settings = config["bootstrap"]
    intervals, summaries = [], {}
    for cohort in settings["cohorts"]:
        mask = masks[cohort]
        columns, labels = [], []
        for name in [*names, "Fixed-seed metric mean"]:
            for baseline in settings["baselines"]:
                for metric_index, metric in enumerate(settings["metrics"]):
                    columns.append((values[name] - values[baseline])[mask, metric_index])
                    labels.append({"method": name, "baseline": baseline, "metric": metric,
                                   "cohort": cohort})
        bootstrap = clustered_mean_interval(
            np.column_stack(columns), users[mask], replicates=settings["replicates"],
            seed=settings["seed"], confidence=settings["confidence"])
        for index, label in enumerate(labels):
            intervals.append({
                **label, **{k: v for k, v in bootstrap.items() if k not in {"estimate", "low", "high"}},
                "estimate": bootstrap["estimate"][index], "low": bootstrap["low"][index],
                "high": bootstrap["high"][index], "simultaneous": False,
            })
        summaries[cohort] = {}
        for index, metric in enumerate(settings["metrics"]):
            points = [float(values[name][mask, index].mean()) for name in names]
            summaries[cohort][metric] = {
                "values": points, "mean": statistics.mean(points),
                "sample_std": statistics.stdev(points), "seeds": config["seeds"],
            }
        print(json.dumps({"phase": "bootstrap", "cohort": cohort,
                          "users": bootstrap["users"], "replicates": bootstrap["replicates"]}), flush=True)
    results = {
        "status": "passed", "replication_id": series["replication_id"],
        "source_series_sha256": file_sha(run / "series.json"),
        "analysis_source_sha256": file_sha(Path(__file__)),
        "bootstrap_source_sha256": file_sha(Path(__file__).with_name("uncertainty.py")),
        "seed_summary": summaries, "intervals": intervals, "trace_audits": trace_audits,
        "audited_queries": sum(a["queries"] for a in trace_audits),
        "audited_candidate_checks": sum(a["candidate_checks"] for a in trace_audits),
        "original_seed17_test_rankings_identical": True,
        "original_source_unchanged": file_sha(source / "series.json") == config["source_series_sha256"],
        "test_users": len(set(users)), "test_queries": len(queries),
        "limits": config["limits"] + [
            "48 marginal intervals, not multiplicity-adjusted simultaneous evidence",
            "fixed-seed metric mean is not an ensemble recommender",
            "cluster bootstrap assumes users are sampling units; shared time/item effects remain",
        ],
    }
    output = Path(config["report_directory"])
    (output / "uncertainty.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    render(series, results)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.run)
    print(json.dumps({key: result[key] for key in ("status", "audited_queries", "audited_candidate_checks")}))
