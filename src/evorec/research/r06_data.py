"""R06 input provenance, paired candidate construction and private source traces."""
import csv
import gzip
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import torch

from evorec.research.baselines import ItemCF
from evorec.research.content import ContentFeatures, ContentPredictor
from evorec.research.content_data import selected_bucket
from evorec.research.multi_interest import MultiInterestPredictor, merge_content
from evorec.research.protocol import AvailableAt
from evorec.research.ranker import PoolInputs, build_pool_inputs, target_positions
from evorec.research.ranker_data import load_protocols, prefix_protocol, read, training_signature
from evorec.research.replicate_ranker import checked_file
from evorec.research.runner import file_sha
from evorec.research.training_baselines import RecentPopular, CollaborativeBlend


def record(path):
    return {"path_from_project_root": path.as_posix(), "sha256": file_sha(path)}


def load_inputs(config):
    source = Path(config["source_run"])
    replica = Path(config["source_replication"])
    if file_sha(source / "series.json") != config["source_series_sha256"]:
        raise ValueError("original R05 source changed")
    if file_sha(replica / "series.json") != config["source_replication_sha256"]:
        raise ValueError("R05 replication changed")
    original, replication = read(source / "series.json"), read(replica / "series.json")
    if original["status"] != "completed" or replication["status"] != "completed":
        raise ValueError("source stages are incomplete")
    for key in ("content", "model", "folds", "train_end_ms", "validation_end_ms",
                "training_run", "history_limit", "encoder_fit_end_ms", "positive_rating_min",
                "learning_rate", "weight_decay", "max_epochs", "patience", "batch_size"):
        if config[key] != original["configuration"][key]:
            raise ValueError("frozen training or feature setting changed: " + key)
    if config["stage"] != "R06-multi-interest" or config["sample"]["bucket"] != 4:
        raise ValueError("wrong registered R06 stage")
    parent, query, overlaps = load_protocols(config)
    users = {e.user_id for e in query.events}
    for stage in ("r05",):
        with Path(f"datasets/video_games_{stage}.csv").open(encoding="utf-8", newline="") as stream:
            overlaps[stage] = len(users & {row["user_id"] for row in csv.DictReader(stream)})
    if any(overlaps.values()):
        raise ValueError("R06 query users overlap prior stages")
    if query.manifest["selection"] != config["sample"]:
        raise ValueError("sample selection differs")
    if not all(selected_bucket(user, set(), config["sample"]["denominator"], 4) for user in users):
        raise ValueError("sample contains users outside the registered bucket")
    query.fingerprint = {**query.fingerprint, "protocol": "r06-paired-multi-interest-v1",
                         "frozen_series_sha256": config["source_series_sha256"],
                         "frozen_replication_sha256": config["source_replication_sha256"]}
    query.protocol_id = hashlib.sha256(json.dumps(query.fingerprint, sort_keys=True).encode()).hexdigest()[:16]
    directory = source / "content-encoder"
    for name, digest in original["content_encoder"]["files"].items():
        if file_sha(directory / name) != digest:
            raise ValueError("encoder input changed")
    features = ContentFeatures(read(directory / "items.json"), np.load(directory / "vectors.npy"))
    if features.fingerprint != original["feature_fingerprint"]:
        raise ValueError("encoder vectors differ")
    frozen = {}
    for seed in config["seeds"]:
        trial = next(t for t in replication["trials"] if t["seed"] == seed)
        checked_file(trial["checkpoint"])
        frozen[seed] = trial["checkpoint"]
    return parent, query, overlaps, features, original, frozen


def group_masks(queries):
    cold = np.array([q.target_available and q.target_model_cold for q in queries], dtype=bool)
    history = np.array([bool(q.history) for q in queries], dtype=bool)
    return {"all_positive_events": np.ones(len(queries), dtype=bool),
            "history_present": history, "cold_user": ~history,
            "model_cold_available": cold, "history_cold_available": cold & history,
            "no_history_cold_available": cold & ~history}


def diagnostics(pool, features, queries):
    positions = target_positions(pool, features, queries)
    counts = (pool.items > 0).sum(axis=1)
    return {name: {"n": int(mask.sum()), "target_in_pool": int(((positions >= 0) & mask).sum()),
                   "pool_recall": float((positions[mask] >= 0).mean()) if mask.any() else None,
                   "mean_pool_candidates": float(counts[mask].mean()) if mask.any() else None}
            for name, mask in group_masks(queries).items()}


def prepare_pair(protocol, features, queries, config, *, device="cuda"):
    if (config["retrieval_k"] != 200 or config["pool_k"] != 400
            or config["multi_interest"]["provider_k"] != 200 or config["multi_interest"]["content_k"] != 200):
        raise ValueError("R06 requires fixed provider and pool budgets")
    if device.startswith("cuda"):
        torch.cuda.reset_peak_memory_stats()
    started = time.perf_counter()
    prior = RecentPopular(365).fit(protocol.train, protocol.config["train_end_ms"], config["positive_rating_min"])
    core = ItemCF(max_user_items=100, neighbors=100).fit(protocol.train, config["positive_rating_min"])
    blend = CollaborativeBlend(core, prior, .25)
    cf = [blend.rank(q.history, q.seen, AvailableAt(protocol.catalog, q.timestamp_ms), 200) for q in queries]
    cost = {"cf_fit_and_rank_seconds": time.perf_counter()-started}
    tick = time.perf_counter()
    predictor = ContentPredictor(features, protocol.catalog, prior, device=device,
                                 batch_size=config["evaluation_batch_size"], decay=config["content"]["history_decay"])
    centroid = predictor.rank_many(queries, k=200)
    if device.startswith("cuda"):
        torch.cuda.synchronize()
    cost["centroid"] = {"wall_seconds": time.perf_counter()-tick,
                        "dense_vector_dot_products": len(queries)*len(features.items)}
    del predictor
    settings = config["multi_interest"]
    tick = time.perf_counter()
    predictor = MultiInterestPredictor(features, protocol.catalog, prior, device=device,
                                      batch_size=config["evaluation_batch_size"],
                                      recent_events=settings["recent_events"], interest_decay=settings["decay"])
    interests = predictor.rank_many(queries, k=200)
    if device.startswith("cuda"):
        torch.cuda.synchronize()
    cost["multi_interest"] = {**predictor.last_cost, "including_setup_wall_seconds": time.perf_counter()-tick}
    del predictor
    tick = time.perf_counter()
    mixed = merge_content(centroid, interests, k=200, weight=settings["centroid_weight"],
                          constant=settings["rrf_constant"])
    pools = {name: build_pool_inputs(features, protocol.catalog, protocol.train_items, blend.priors,
                                     queries, cf, content, pool_k=400,
                                     decay=config["content"]["history_decay"], constant=config["rrf_constant"])
             for name, content in (("A", centroid), ("B", mixed))}
    cost["fusion_and_both_feature_pools_seconds"] = time.perf_counter()-tick
    cost["total_pair_seconds"] = time.perf_counter()-started
    cost["gpu_peak_allocated_bytes"] = torch.cuda.max_memory_allocated() if device.startswith("cuda") else None
    cost["reuse"] = "A and B share CF and centroid computation; B adds multi-interest and fusion"
    return pools, {"cf": cf, "centroid": centroid, "multi": interests, "mixed": mixed}, cost


def write_sources(path, pools, providers, features, queries):
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for row, q in enumerate(queries):
            entry = {"query_id": q.query_id, "timestamp_ms": q.timestamp_ms, "target": q.target,
                     "history": q.history, "target_available": q.target_available,
                     "target_model_cold": q.target_model_cold,
                     "providers": {name: rankings[row].items for name, rankings in providers.items()},
                     "pools": {name: [features.items[int(i)-1] for i in pool.items[row] if i > 0]
                               for name, pool in pools.items()}}
            stream.write(json.dumps(entry, separators=(",", ":"))+"\n")
    return record(path)


def cache_pair(output, prefix, protocol, features, queries, config):
    pools, providers, cost = prepare_pair(protocol, features, queries, config)
    result = {"cost": cost, "diagnostics": {name: diagnostics(p, features, queries) for name, p in pools.items()},
              "source_trace": write_sources(output / (prefix+"-sources.jsonl.gz"), pools, providers, features, queries),
              "caches": {}}
    for name, pool in pools.items():
        path = output / (prefix+"-"+name+".npz")
        pool.save(path, targets=target_positions(pool, features, queries),
                  query_ids=np.array([q.query_id for q in queries]))
        result["caches"][name] = record(path)
    return pools, providers, result


def training_queries(parent, fold, config):
    p = prefix_protocol(parent, fold["start_ms"], fold["end_ms"])
    all_queries = [q for q in p.queries("validation") if q.history]
    sampled = sorted(all_queries, key=lambda q: hashlib.sha256(q.query_id.encode()).digest())[:config["max_training_queries_per_fold"]]
    return p, sorted(sampled, key=lambda q: (q.timestamp_ms, q.query_id)), len(all_queries)


def prepare_training(parent, features, config, output):
    collected = {name: {"pools": [], "targets": [], "cold": [], "query_ids": []} for name in ("A", "B")}
    folds = []
    for fold in config["folds"]:
        p, queries, count = training_queries(parent, fold, config)
        pools, providers, cost = prepare_pair(p, features, queries, config)
        result = {**fold, "all_history_queries": count, "sampled_queries": len(queries),
                  "statistics_rows": len(p.train), "statistics_signature": training_signature(p.train), "cost": cost,
                  "source_trace": write_sources(output / ("train-"+fold["name"]+"-sources.jsonl.gz"),
                                                pools, providers, features, queries), "arms": {}}
        for name, pool in pools.items():
            positions = target_positions(pool, features, queries)
            effective = np.linalg.norm(pool.contexts, axis=1) > 1e-8
            used = np.flatnonzero((positions >= 0) & effective)
            data = collected[name]
            data["pools"].append(pool.subset(used))
            data["targets"].append(positions[used])
            cold = np.array([queries[i].target_model_cold for i in used], dtype=bool)
            data["cold"].append(cold)
            data["query_ids"].extend(queries[i].query_id for i in used)
            result["arms"][name] = {"examples": len(used), "cold_examples": int(cold.sum()),
                                    "candidate_misses": int((positions < 0).sum()),
                                    "unrepresented_history": int(((positions >= 0) & ~effective).sum()),
                                    "diagnostics": diagnostics(pool, features, queries)}
        folds.append(result)
        print(json.dumps({"phase": "training_candidates", "fold": fold["name"],
                          "examples": {k: v["examples"] for k, v in result["arms"].items()}}), flush=True)
    output_data, records = {}, {}
    for name, data in collected.items():
        pool = PoolInputs.join(data["pools"])
        labels, cold = np.concatenate(data["targets"]), np.concatenate(data["cold"])
        if len(labels) < 100 or int(cold.sum()) < 10:
            raise ValueError("insufficient retrieved training positives")
        path = output / ("training-"+name+".npz")
        pool.save(path, targets=labels, cold=cold, query_ids=np.array(data["query_ids"]))
        output_data[name] = (pool, labels, cold)
        records[name] = {**record(path), "examples": len(labels), "cold_examples": int(cold.sum())}
    return output_data, records, folds
