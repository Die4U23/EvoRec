"""Run the registered R06 factorial experiment; test opens after validation freeze."""
import argparse
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch

from evorec.research.content_data import prepare
from evorec.research.neural import configure_seed
from evorec.research.protocol import summarize
from evorec.research.ranker import ResidualListRanker, SCALAR_NAMES, listwise_loss, predict, load_ranker, baseline_rankings
from evorec.research.ranker_data import read
from evorec.research.r06_data import load_inputs, prepare_training, cache_pair, record, group_masks
from evorec.research.replicate_ranker import checked_file
from evorec.research.runner import clean_experiment_snapshot, file_sha, peak_memory_bytes
from evorec.research.train import write_trace


def now():
    return datetime.now(timezone.utc).isoformat()


def save(series, output):
    path = output / "series.json.tmp"
    path.write_text(json.dumps(series, indent=2)+"\n", encoding="utf-8")
    path.replace(output / "series.json")


def code_snapshot():
    return clean_experiment_snapshot()


def extended_metrics(protocol, queries, rankings):
    masks = group_masks(queries)
    return {name: summarize(protocol, [q for q, keep in zip(queries, mask) if keep],
                            [r for r, keep in zip(rankings, mask) if keep])["cohorts"]["all_positive_events"]
            for name, mask in masks.items() if name in {"history_cold_available", "no_history_cold_available"}}


def evaluate(name, rankings, protocol, queries, output, split, *, pool_arm, seconds=None):
    return {"name": name, "pool_arm": pool_arm, "metrics": summarize(protocol, queries, rankings),
            "extra_cohorts": extended_metrics(protocol, queries, rankings),
            "ranking_wall_seconds": seconds,
            "trace": write_trace(output / (split+"-"+name+".jsonl.gz"), queries, rankings)}


def fit(arm, seed, training, validation, pool, protocol, features, series, output):
    config = series["configuration"]
    configure_seed(seed)
    torch.cuda.reset_peak_memory_stats()
    train, targets, cold = training
    model = ResidualListRanker(features.vectors.shape[1], **config["model"]).to("cuda")
    optimizer = torch.optim.AdamW(model.parameters(), lr=config["learning_rate"], weight_decay=config["weight_decay"])
    vectors = torch.from_numpy(np.vstack((np.zeros((1, features.vectors.shape[1]), dtype=np.float32), features.vectors))).to("cuda")
    name = arm+"-adapted-s"+str(seed)
    trial = {"name": name, "arm": arm, "pool_arm": "B" if arm == "C" else "A",
             "seed": seed, "status": "training", "history": []}
    series["trials"].append(trial)
    directory = output / name
    directory.mkdir()
    checkpoint = directory / "best.pt"
    rng = np.random.default_rng(seed)
    best, stale = -1., 0
    started = time.perf_counter()
    for epoch in range(1, config["max_epochs"]+1):
        tick = time.perf_counter()
        model.train()
        numerator = denominator = 0.
        order = rng.permutation(len(targets))
        for start in range(0, len(order), config["batch_size"]):
            indices = order[start:start+config["batch_size"]]
            ids = torch.from_numpy(train.items[indices].astype(np.int64)).to("cuda")
            contexts = torch.from_numpy(train.contexts[indices]).to("cuda")
            scalars = torch.from_numpy(train.scalars[indices]).to("cuda")
            labels = torch.from_numpy(targets[indices]).to("cuda")
            cold_batch = torch.from_numpy(cold[indices]).to("cuda")
            optimizer.zero_grad(set_to_none=True)
            loss = listwise_loss(model(contexts, vectors[ids], scalars, ids > 0), labels, cold_batch, 4)
            if not torch.isfinite(loss):
                raise ValueError("non-finite loss")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1., error_if_nonfinite=True)
            optimizer.step()
            weight = float(np.where(cold[indices], 4, 1).sum())
            numerator += loss.item()*weight
            denominator += weight
        metrics = summarize(protocol, validation, predict(model, pool, features, batch_size=config["evaluation_batch_size"]))
        ndcg = metrics["cohorts"]["all_positive_events"]["ndcg@10"]
        trial["history"].append({"epoch": epoch, "train_loss": numerator/denominator, "validation_ndcg@10": ndcg,
                                  "validation_cold_recall@20": metrics["cohorts"]["model_cold_available"]["recall@20"],
                                  "wall_seconds": time.perf_counter()-tick})
        if ndcg > best:
            best, stale = ndcg, 0
            trial.update({"best_epoch": epoch, "best_validation": metrics})
            payload = {"protocol_id": protocol.protocol_id, "feature_fingerprint": features.fingerprint,
                       "scalar_names": list(SCALAR_NAMES), "model_config": config["model"], "seed": seed,
                       "epoch": epoch, "arm": arm,
                       "training_cache_sha256": series["training_caches"][trial["pool_arm"]]["sha256"],
                       "state_dict": {key: value.detach().cpu() for key, value in model.state_dict().items()}}
            temporary = checkpoint.with_suffix(".tmp")
            torch.save(payload, temporary)
            temporary.replace(checkpoint)
        else:
            stale += 1
        save(series, output)
        print(json.dumps({"phase": "epoch", "trial": name, **trial["history"][-1]}), flush=True)
        if stale >= config["patience"]:
            break
    trial.update({"status": "completed", "checkpoint": record(checkpoint), "wall_seconds": time.perf_counter()-started,
                  "gpu_peak_allocated_bytes": torch.cuda.max_memory_allocated()})
    del model, optimizer, vectors
    torch.cuda.empty_cache()
    restored = load_ranker(checked_file(trial["checkpoint"]), protocol.protocol_id, features, config["model"])
    rankings = predict(restored, pool, features, batch_size=config["evaluation_batch_size"])
    if summarize(protocol, validation, rankings) != trial["best_validation"]:
        raise ValueError("checkpoint reload changed validation")
    trial["checkpoint_reload_verified"] = True
    del restored
    series["validation_results"].append(evaluate(name, rankings, protocol, validation, output, "validation",
                                                 pool_arm=trial["pool_arm"]))
    save(series, output)


def select_method(results, config):
    lookup = {row["name"]: row for row in results}
    selection = config["selection"]
    def metric(name, cohort, metric_name):
        return lookup[name]["metrics"]["cohorts"][cohort][metric_name]
    floor = selection["ndcg_min_ratio"] * max(metric(name, "all_positive_events", "ndcg@10")
                                             for name in selection["floor_baselines"])
    names = selection["declaration_order"]
    eligible = [name for name in names if metric(name, "all_positive_events", "ndcg@10") >= floor]
    winner = max(eligible, key=lambda name: (metric(name, "model_cold_available", "recall@20"),
                                             metric(name, "all_positive_events", "ndcg@10"), -names.index(name)))
    return {"selected_method": winner, "validation_ndcg_floor": floor,
            "eligible_methods": eligible, "selection_finished_at": now()}


def evaluate_fixed(split, pools, providers, queries, protocol, features, original, frozen, series, output):
    result = series[split+"_results"]
    for name, rankings, arm in (("CF-blend", providers["cf"], "A"),
                                ("A-RRF", baseline_rankings(pools["A"], features), "A"),
                                ("B-RRF", baseline_rankings(pools["B"], features), "B")):
        result.append(evaluate(name, rankings, protocol, queries, output, split, pool_arm=arm))
    for seed in series["configuration"]["seeds"]:
        model = load_ranker(checked_file(frozen[seed]), original["protocol_id"], features, original["configuration"]["model"])
        for arm in ("A", "B"):
            torch.cuda.synchronize()
            tick = time.perf_counter()
            rankings = predict(model, pools[arm], features, batch_size=series["configuration"]["evaluation_batch_size"])
            torch.cuda.synchronize()
            result.append(evaluate(f"{arm}-frozen-s{seed}", rankings, protocol, queries, output, split,
                                   pool_arm=arm, seconds=time.perf_counter()-tick))
        del model
    save(series, output)


def open_test(series, protocol):
    expected_trials = {(arm, seed) for arm in ("C", "D") for seed in (17, 29, 43)}
    expected_names = {"CF-blend", "A-RRF", "B-RRF"} | {
        f"{arm}-{kind}-s{seed}" for arm, kind in (("A", "frozen"), ("B", "frozen"), ("C", "adapted"), ("D", "adapted"))
        for seed in (17, 29, 43)}
    if (len(series["trials"]) != 6 or {(t["arm"], t["seed"]) for t in series["trials"]} != expected_trials
            or not all(t.get("checkpoint_reload_verified") and t["status"] == "completed" for t in series["trials"])
            or len(series["validation_results"]) != 15
            or {r["name"] for r in series["validation_results"]} != expected_names
            or series.get("selected_method") not in expected_names or not series.get("selection_finished_at")):
        raise ValueError("all checkpoints, validation traces and selection must freeze before test")
    return protocol.queries("test", test_authorized=True)


def run(config_path, output):
    config = read(config_path)
    provenance = code_snapshot()
    if not torch.cuda.is_available():
        raise RuntimeError("registered run requires CUDA")
    configure_seed(17)
    if not Path(config["dataset_path"]).exists():
        prepare(config)
    parent, protocol, overlaps, features, original, frozen = load_inputs(config)
    output.mkdir(parents=True, exist_ok=False)
    (output / "source").mkdir()
    for path in Path(__file__).parent.glob("*.py"):
        shutil.copyfile(path, output / "source" / path.name)
    shutil.copyfile(config_path, output / "configuration.json")
    series = {"status": "preparing", "started_at": now(), "configuration": config,
              "configuration_sha256": file_sha(config_path), "protocol_id": protocol.protocol_id,
              "protocol": protocol.fingerprint, "user_overlaps": overlaps,
              "data_provenance": protocol.manifest, "model_training_provenance": parent.manifest,
              "metadata_provenance": protocol.metadata_manifest, "feature_fingerprint": features.fingerprint,
              "frozen_checkpoints": frozen, "code": {**provenance, "source_sha256": {
                  p.name: file_sha(p) for p in (output / "source").glob("*.py")}},
              "device": torch.cuda.get_device_name(), "torch": torch.__version__,
              "trials": [], "validation_results": [], "test_results": []}
    save(series, output)
    try:
        training, caches, folds = prepare_training(parent, features, config, output)
        series.update({"training_caches": caches, "training_folds": folds})
        # Reconstructed A inputs must be identical to the original candidate pipeline.
        with np.load(checked_file(original["training_cache"]), allow_pickle=False) as previous:
            with np.load(checked_file(caches["A"]), allow_pickle=False) as current:
                for key in previous.files:
                    np.testing.assert_array_equal(previous[key], current[key], err_msg="A training cache drift: "+key)
        series["A_training_matches_original"] = True
        validation = protocol.queries("validation")
        vpools, providers, info = cache_pair(output, "validation", protocol, features, validation, config)
        series["validation_candidates"] = info
        evaluate_fixed("validation", vpools, providers, validation, protocol, features, original, frozen, series, output)
        del providers
        series["status"] = "training"
        for arm, pool_name in (("D", "A"), ("C", "B")):
            for seed in config["seeds"]:
                fit(arm, seed, training[pool_name], validation, vpools[pool_name],
                    protocol, features, series, output)
        series.update(select_method(series["validation_results"], config))
        series["checkpoints_frozen_at"] = now()
        save(series, output)
        del training, vpools, validation
        series.update({"status": "final_test", "test_opened_at": now()})
        save(series, output)
        test = open_test(series, protocol)
        pools, providers, info = cache_pair(output, "test", protocol, features, test, config)
        series["test_candidates"] = info
        evaluate_fixed("test", pools, providers, test, protocol, features, original, frozen, series, output)
        for trial in series["trials"]:
            model = load_ranker(checked_file(trial["checkpoint"]), protocol.protocol_id, features, config["model"])
            torch.cuda.synchronize()
            tick = time.perf_counter()
            rankings = predict(model, pools[trial["pool_arm"]], features, batch_size=config["evaluation_batch_size"])
            torch.cuda.synchronize()
            series["test_results"].append(evaluate(trial["name"], rankings, protocol, test, output, "test",
                                                   pool_arm=trial["pool_arm"], seconds=time.perf_counter()-tick))
            del model
            save(series, output)
        if file_sha(Path(config["source_run"]) / "series.json") != config["source_series_sha256"]:
            raise ValueError("old R05 evidence changed")
        if file_sha(Path(config["source_replication"]) / "series.json") != config["source_replication_sha256"]:
            raise ValueError("old R05 replication changed")
        series.update({"status": "completed", "finished_at": now(), "peak_working_set_bytes": peak_memory_bytes()})
        save(series, output)
    except Exception as error:
        series.update({"status": "failed", "error": f"{type(error).__name__}: {error}"})
        save(series, output)
        raise
    return series


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("research/configs/r06-multi-interest.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    completed = run(args.config, args.output)
    print(json.dumps({"status": completed["status"], "protocol_id": completed["protocol_id"]}))
