"""Replay R06 candidate sources and rankings, then compute registered user intervals."""
import argparse
import gzip
import json
import statistics
from pathlib import Path

import numpy as np
import torch

from evorec.research.neural import configure_seed
from evorec.research.ranker import predict, load_ranker, baseline_rankings, target_positions
from evorec.research.ranker_data import read
from evorec.research.r06_data import load_inputs, prepare_pair, training_queries, group_masks, diagnostics
from evorec.research.replicate_ranker import checked_file
from evorec.research.replication_analysis import trace_values, query_users
from evorec.research.run_multi_interest import extended_metrics, select_method
from evorec.research.runner import file_sha
from evorec.research.uncertainty import clustered_mean_interval


def audit_sources(entry, protocol, queries, features, config):
    pools, providers, _ = prepare_pair(protocol, features, queries, config)
    checks = 0
    with gzip.open(checked_file(entry["source_trace"]), "rt", encoding="utf-8") as stream:
        for row, query in enumerate(queries):
            saved = json.loads(stream.readline())
            identity = {"query_id": query.query_id, "timestamp_ms": query.timestamp_ms, "target": query.target,
                        "history": list(query.history), "target_available": query.target_available,
                        "target_model_cold": query.target_model_cold}
            if any(saved[key] != value for key, value in identity.items()):
                raise ValueError("source trace identity differs")
            if set(saved["providers"]) != set(providers) or set(saved["pools"]) != {"A", "B"}:
                raise ValueError("missing candidate source")
            for name, rankings in providers.items():
                expected = list(rankings[row].items)
                if saved["providers"][name] != expected or len(expected) != len(set(expected)) or len(expected) > 200:
                    raise ValueError("provider replay or budget mismatch: "+name)
                checks += len(expected)
            for name, pool in pools.items():
                expected = [features.items[int(i)-1] for i in pool.items[row] if i > 0]
                if saved["pools"][name] != expected:
                    raise ValueError("union replay mismatch")
                checks += len(expected)
        if stream.readline():
            raise ValueError("extra source rows")
    if "caches" in entry:
        for name, pool in pools.items():
            with np.load(checked_file(entry["caches"][name]), allow_pickle=False) as cached:
                for key in ("items", "contexts", "scalars"):
                    np.testing.assert_array_equal(cached[key], getattr(pool, key))
                np.testing.assert_array_equal(cached["targets"], target_positions(pool, features, queries))
                np.testing.assert_array_equal(cached["query_ids"], [q.query_id for q in queries])
            if entry["diagnostics"][name] != diagnostics(pool, features, queries):
                raise ValueError("pool summary differs")
    return pools, providers, {"queries": len(queries), "candidate_checks": checks, "full_provider_replay": True}


def replay_rankings(result, split, series, pools, providers, features, original):
    name = result["name"]
    if name == "CF-blend":
        rankings = providers["cf"]
    elif name.endswith("RRF"):
        rankings = baseline_rankings(pools[result["pool_arm"]], features)
    elif "-frozen-" in name:
        seed = name.rsplit("s", 1)[1]
        cp = series["frozen_checkpoints"][seed]
        protocol_id = original["protocol_id"]
    else:
        trial = next(t for t in series["trials"] if t["name"] == name)
        cp, protocol_id = trial["checkpoint"], series["protocol_id"]
    if name != "CF-blend" and not name.endswith("RRF"):
        model = load_ranker(checked_file(cp), protocol_id, features, series["configuration"]["model"])
        rankings = predict(model, pools[result["pool_arm"]], features,
                           batch_size=series["configuration"]["evaluation_batch_size"])
        del model
    with gzip.open(checked_file(result["trace"]), "rt", encoding="utf-8") as stream:
        for ranking in rankings:
            if list(ranking.items) != json.loads(stream.readline())["recommendations"]:
                raise ValueError("model ranking replay differs: "+split+"/"+name)
        if stream.readline():
            raise ValueError("extra ranking trace rows")
    return rankings


def analyze(run):
    series = read(run / "series.json")
    if series["status"] != "completed":
        raise ValueError("R06 training and test must finish first")
    config = series["configuration"]
    configure_seed(17)
    parent, protocol, overlaps, features, original, _ = load_inputs(config)
    if protocol.protocol_id != series["protocol_id"] or overlaps != series["user_overlaps"]:
        raise ValueError("protocol or users differ")
    for name, digest in series["code"]["source_sha256"].items():
        if file_sha(run / "source" / name) != digest:
            raise ValueError("training source snapshot changed")
    if not (series["selection_finished_at"] <= series["checkpoints_frozen_at"] <= series["test_opened_at"]):
        raise ValueError("invalid selection/test order")
    selection = select_method(series["validation_results"], config)
    for key in ("selected_method", "validation_ndcg_floor", "eligible_methods"):
        if selection[key] != series[key]:
            raise ValueError("validation selection differs")
    source_audits, trace_audits = [], []
    for fold in series["training_folds"]:
        p, queries, _ = training_queries(parent, fold, config)
        pools, _, audit = audit_sources(fold, p, queries, features, config)
        source_audits.append({"split": fold["name"], **audit})
        for name, pool in pools.items():
            positions = target_positions(pool, features, queries)
            eligible = (positions >= 0) & (np.linalg.norm(pool.contexts, axis=1) > 1e-8)
            cold = np.array([q.target_model_cold for q in queries], dtype=bool)
            stats = fold["arms"][name]
            if int(eligible.sum()) != stats["examples"] or int((eligible & cold).sum()) != stats["cold_examples"]:
                raise ValueError("training eligibility mismatch")
            with np.load(checked_file(series["training_caches"][name]), allow_pickle=False) as cache:
                indices = {identity: i for i, identity in enumerate(cache["query_ids"].tolist())}
                used = np.flatnonzero(eligible)
                rows = [indices[queries[i].query_id] for i in used]
                for key in ("items", "contexts", "scalars"):
                    np.testing.assert_array_equal(cache[key][rows], getattr(pool, key)[used])
                np.testing.assert_array_equal(cache["targets"][rows], positions[used])
                np.testing.assert_array_equal(cache["cold"][rows], cold[used])
        del pools
        print(json.dumps({"phase": "candidate_audit", **source_audits[-1]}), flush=True)
    for trial in series["trials"]:
        saved = torch.load(checked_file(trial["checkpoint"]), weights_only=True, map_location="cpu")
        if (saved["epoch"] != trial["best_epoch"] or saved["seed"] != trial["seed"] or saved["arm"] != trial["arm"]
                or saved["training_cache_sha256"] != series["training_caches"][trial["pool_arm"]]["sha256"]):
            raise ValueError("checkpoint training binding differs")
        if max(trial["history"], key=lambda r: r["validation_ndcg@10"])["epoch"] != trial["best_epoch"]:
            raise ValueError("checkpoint epoch differs from validation rule")
    values = {}
    test_queries = None
    for split in ("validation", "test"):
        queries = protocol.queries(split, test_authorized=split == "test")
        pools, providers, audit = audit_sources(series[split+"_candidates"], protocol, queries, features, config)
        source_audits.append({"split": split, **audit})
        if len(series[split+"_results"]) != 15:
            raise ValueError("incomplete method results")
        for result in series[split+"_results"]:
            scores, audit = trace_values(result["trace"], queries, protocol, result["metrics"],
                                         pools[result["pool_arm"]].items, features.items)
            rankings = replay_rankings(result, split, series, pools, providers, features, original)
            if result["extra_cohorts"] != extended_metrics(protocol, queries, rankings):
                raise ValueError("cold/history subgroup differs")
            # Original trace format omits CF provider attribution; private provider replay is authoritative.
            if split == "test":
                values[result["name"]] = scores
            trace_audits.append({"split": split, "name": result["name"], **audit})
        if split == "test":
            test_queries = queries
        del pools, providers
        print(json.dumps({"phase": "ranking_audit", "split": split, "methods": 15}), flush=True)
    queries = test_queries
    users = query_users(protocol.events, queries, config)
    families = ("A-frozen", "B-frozen", "C-adapted", "D-adapted")
    for name in families:
        values[name] = np.stack([values[f"{name}-s{seed}"] for seed in config["seeds"]]).mean(axis=0)
    intervals, summaries = [], {}
    for cohort in config["bootstrap"]["cohorts"]:
        mask = group_masks(queries)[cohort]
        labels, columns = [], []
        for method, baseline in config["bootstrap"]["contrasts"]:
            for index, metric in enumerate(config["bootstrap"]["metrics"]):
                labels.append({"cohort": cohort, "method": method, "baseline": baseline, "metric": metric})
                columns.append((values[method]-values[baseline])[mask, index])
        settings = config["bootstrap"]
        if len(np.unique(users[mask])) < 2:
            intervals.extend({**label, "status": "not_estimable", "requests": int(mask.sum()),
                              "users": len(np.unique(users[mask]))} for label in labels)
        else:
            sampled = clustered_mean_interval(np.column_stack(columns), users[mask], replicates=settings["replicates"],
                                               seed=settings["seed"], confidence=settings["confidence"])
            for index, label in enumerate(labels):
                intervals.append({**label, "status": "estimated",
                                  **{k: v for k, v in sampled.items() if k not in {"estimate", "low", "high"}},
                                  "estimate": sampled["estimate"][index], "low": sampled["low"][index],
                                  "high": sampled["high"][index], "simultaneous": False})
        summaries[cohort] = {}
        for name in families:
            summaries[cohort][name] = {}
            for index, metric in enumerate(settings["metrics"]):
                points = [float(values[f"{name}-s{seed}"][mask, index].mean()) for seed in config["seeds"]] if mask.any() else []
                summaries[cohort][name][metric] = {"values": points, "seeds": config["seeds"],
                    "mean": statistics.mean(points) if points else None,
                    "sample_std": statistics.stdev(points) if points else None}
        print(json.dumps({"phase": "bootstrap", "cohort": cohort, "requests": int(mask.sum())}), flush=True)
    result = {"status": "passed", "protocol_id": series["protocol_id"], "series_sha256": file_sha(run / "series.json"),
              "analysis_source_sha256": file_sha(Path(__file__)), "bootstrap_source_sha256": file_sha(Path(__file__).with_name("uncertainty.py")),
              "seed_summary": summaries, "intervals": intervals, "source_audits": source_audits, "trace_audits": trace_audits,
              "all_candidate_sources_and_models_replayed": True,
              "audited_source_queries": sum(a["queries"] for a in source_audits),
              "audited_ranking_queries": sum(a["queries"] for a in trace_audits),
              "audited_candidate_checks": sum(a["candidate_checks"] for a in source_audits+trace_audits),
              "limits": config["limits"]}
    path = run / "analysis.json"
    path.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.run)
    print(json.dumps({"status": result["status"], "intervals": len(result["intervals"])}))
