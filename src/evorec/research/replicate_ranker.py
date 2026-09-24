"""Replay R05 seed 17 and replicate the frozen cold-weighted setting."""
import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path

import numpy as np
import torch

from evorec.research.content import ContentFeatures
from evorec.research.neural import configure_seed
from evorec.research.protocol import summarize
from evorec.research.ranker import (
    PoolInputs, ResidualListRanker, SCALAR_NAMES, listwise_loss, load_ranker,
    predict, target_positions,
)
from evorec.research.ranker_data import load_protocols, read
from evorec.research.replication_report import render
from evorec.research.reporting import write_atomic
from evorec.research.runner import clean_experiment_snapshot, file_sha, peak_memory_bytes
from evorec.research.train import write_trace
from evorec.research.train_ranker import now


def checked_file(record):
    path = Path(record["path_from_project_root"])
    if file_sha(path) != record["sha256"]:
        raise ValueError(f"source fingerprint differs: {path}")
    return path


def load_cache(record):
    """Read each compressed array once, while preserving original row order."""
    path = checked_file(record)
    with np.load(path, allow_pickle=False) as saved:
        data = {name: saved[name] for name in saved.files}
    return PoolInputs(data["items"], data["contexts"], data["scalars"]), data


def checkpoint_states_equal(first, second):
    left, right = first["state_dict"], second["state_dict"]
    return (left.keys() == right.keys()
            and all(torch.equal(left[key], right[key]) for key in left))


def save(series, output):
    write_atomic(output / "series.json", json.dumps(series, indent=2) + "\n")
    render(series)


def fit(seed, protocol, features, train, data, validation, pool, series, output):
    """Same initialization, optimizer, minibatches and early stopping as R05."""
    config = series["source_configuration"]
    setting = series["configuration"]["setting"]
    configure_seed(seed)
    torch.cuda.reset_peak_memory_stats()
    model = ResidualListRanker(features.vectors.shape[1], **config["model"]).to("cuda")
    optimizer = torch.optim.AdamW(model.parameters(), lr=config["learning_rate"],
                                  weight_decay=config["weight_decay"])
    vectors = torch.from_numpy(np.vstack((
        np.zeros((1, features.vectors.shape[1]), dtype=np.float32), features.vectors
    ))).to("cuda")
    targets, cold = data["targets"], data["cold"]
    rng = np.random.default_rng(seed)
    trial = {"name": f"ColdListMLP-s{seed}", "seed": seed, "setting": setting,
             "status": "training", "history": []}
    series["trials"].append(trial)
    directory = output / trial["name"]
    directory.mkdir()
    checkpoint = directory / "best.pt"
    best, stale = -1.0, 0
    started = time.perf_counter()
    for epoch in range(1, config["max_epochs"] + 1):
        tick = time.perf_counter()
        model.train()
        numerator = denominator = 0.0
        order = rng.permutation(len(targets))
        for start in range(0, len(order), config["batch_size"]):
            indices = order[start:start + config["batch_size"]]
            ids = torch.from_numpy(train.items[indices].astype(np.int64)).to("cuda")
            contexts = torch.from_numpy(train.contexts[indices]).to("cuda")
            scalars = torch.from_numpy(train.scalars[indices]).to("cuda")
            labels = torch.from_numpy(targets[indices]).to("cuda")
            cold_batch = torch.from_numpy(cold[indices]).to("cuda")
            optimizer.zero_grad(set_to_none=True)
            logits = model(contexts, vectors[ids], scalars, ids > 0)
            loss = listwise_loss(logits, labels, cold_batch, setting["cold_weight"])
            if not torch.isfinite(loss):
                raise ValueError("non-finite training loss")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0, error_if_nonfinite=True)
            optimizer.step()
            weight = float(np.where(cold[indices], setting["cold_weight"], 1).sum())
            numerator += loss.item() * weight
            denominator += weight
        metrics = summarize(protocol, validation, predict(
            model, pool, features, batch_size=config["evaluation_batch_size"]))
        ndcg = metrics["cohorts"]["all_positive_events"]["ndcg@10"]
        row = {
            "epoch": epoch, "train_loss": numerator / denominator,
            "validation_ndcg@10": ndcg,
            "validation_cold_recall@20": metrics["cohorts"]["model_cold_available"]["recall@20"],
            "wall_seconds": time.perf_counter() - tick,
        }
        trial["history"].append(row)
        if ndcg > best:
            best, stale = ndcg, 0
            trial.update({"best_epoch": epoch, "best_validation": metrics})
            payload = {
                "protocol_id": protocol.protocol_id, "feature_fingerprint": features.fingerprint,
                "scalar_names": list(SCALAR_NAMES), "model_config": config["model"],
                "setting": setting, "seed": seed, "epoch": epoch,
                "training_cache_sha256": series["source_inputs"]["training_cache"]["sha256"],
                "state_dict": {key: value.detach().cpu() for key, value in model.state_dict().items()},
            }
            temporary = checkpoint.with_suffix(".tmp")
            torch.save(payload, temporary)
            temporary.replace(checkpoint)
        else:
            stale += 1
        save(series, output)
        print(json.dumps({"phase": "epoch", "trial": trial["name"], **row}), flush=True)
        if stale >= config["patience"]:
            break
    trial.update({
        "status": "completed", "wall_seconds": time.perf_counter() - started,
        "checkpoint": {"path_from_project_root": checkpoint.as_posix(), "sha256": file_sha(checkpoint)},
        "gpu_peak_allocated_bytes": torch.cuda.max_memory_allocated(),
    })
    del model, optimizer, vectors
    torch.cuda.empty_cache()
    return trial


def run(config_path, output):
    config = read(config_path)
    if config["stage"] != "R05-cold-replication" or config["seeds"] != [17, 29, 43]:
        raise ValueError("unexpected registered experiment")
    source = Path(config["source_run"])
    source_file = source / "series.json"
    if file_sha(source_file) != config["source_series_sha256"]:
        raise ValueError("original R05 series changed")
    original = read(source_file)
    if original["status"] != "completed" or original["protocol_id"] != config["source_protocol_id"]:
        raise ValueError("source experiment mismatch")
    source_config = original["configuration"]
    expected_setting = next(t for t in source_config["trials"] if t["name"] == "ColdListMLP")
    if config["setting"] != expected_setting:
        raise ValueError("replication setting differs from original")
    for name in ("ranker.py", "ranker_data.py", "train_ranker.py"):
        if file_sha(Path(__file__).parent / name) != original["code"]["source_sha256"][name]:
            raise ValueError("original ranking implementation drift")
    provenance = clean_experiment_snapshot()
    if not torch.cuda.is_available():
        raise RuntimeError("registered training requires CUDA")
    _, protocol, overlaps = load_protocols(source_config)
    if protocol.protocol_id != original["protocol_id"]:
        raise ValueError("reconstructed evaluation protocol differs")
    encoder = source / "content-encoder"
    for name, expected in original["content_encoder"]["files"].items():
        if file_sha(encoder / name) != expected:
            raise ValueError("frozen encoder changed")
    features = ContentFeatures(read(encoder / "items.json"), np.load(encoder / "vectors.npy"))
    if features.fingerprint != original["feature_fingerprint"]:
        raise ValueError("feature fingerprint differs")
    train, training_data = load_cache(original["training_cache"])
    validation_pool, validation_data = load_cache(original["validation_cache"])
    validation = protocol.queries("validation")
    np.testing.assert_array_equal(validation_data["targets"],
                                  target_positions(validation_pool, features, validation))
    if len(training_data["targets"]) != original["training_examples"]:
        raise ValueError("training row count differs")
    output.mkdir(parents=True, exist_ok=False)
    (output / "source").mkdir()
    for path in Path(__file__).parent.glob("*.py"):
        shutil.copyfile(path, output / "source" / path.name)
    shutil.copyfile(config_path, output / "configuration.json")
    series = {
        "status": "training", "started_at": now(), "configuration": config,
        "source_configuration": source_config, "protocol_id": protocol.protocol_id,
        "replication_id": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16],
        "source_inputs": {key: original[key] for key in ("training_cache", "validation_cache", "test_cache")},
        "source_series_sha256": file_sha(source_file), "feature_fingerprint": features.fingerprint,
        "code": {**provenance, "source_sha256": {
            p.name: file_sha(p) for p in (output / "source").glob("*.py")}},
        "device": torch.cuda.get_device_name(), "torch": torch.__version__,
        "user_overlaps": overlaps, "training_examples": len(training_data["targets"]),
        "cold_training_examples": int(training_data["cold"].sum()),
        "trials": [], "test_results": [],
    }
    save(series, output)
    try:
        for seed in config["seeds"]:
            trial = fit(seed, protocol, features, train, training_data, validation,
                        validation_pool, series, output)
            checkpoint = checked_file(trial["checkpoint"])
            model = load_ranker(checkpoint, protocol.protocol_id, features, source_config["model"])
            rankings = predict(model, validation_pool, features,
                               batch_size=source_config["evaluation_batch_size"])
            if summarize(protocol, validation, rankings) != trial["best_validation"]:
                raise ValueError("checkpoint reload validation changed")
            trial["checkpoint_reload_verified"] = True
            trial["validation_trace"] = write_trace(
                output / f"validation-{trial['name']}.jsonl.gz", validation, rankings)
            if seed == config["replay_seed"]:
                old = next(t for t in original["trials"] if t["name"] == trial["name"])
                left = torch.load(checked_file(old["checkpoint"]), weights_only=True, map_location="cpu")
                right = torch.load(checkpoint, weights_only=True, map_location="cpu")
                if (not checkpoint_states_equal(left, right) or old["best_epoch"] != trial["best_epoch"]
                        or old["best_validation"] != trial["best_validation"]):
                    raise ValueError("seed 17 original checkpoint replay differs")
                keys = ("epoch", "train_loss", "validation_ndcg@10", "validation_cold_recall@20")
                if ([{k: r[k] for k in keys} for r in old["history"]]
                        != [{k: r[k] for k in keys} for r in trial["history"]]):
                    raise ValueError("seed 17 epoch trajectory differs")
                trial["original_replay_verified"] = True
            del model
            save(series, output)
        del train, training_data, validation_pool, validation_data, validation
        series.update({"status": "replication_test", "checkpoints_frozen_at": now()})
        save(series, output)
        # Old test is only revisited after every replica checkpoint has been frozen.
        queries = protocol.queries("test", test_authorized=True)
        pool, test_data = load_cache(original["test_cache"])
        np.testing.assert_array_equal(test_data["targets"], target_positions(pool, features, queries))
        for trial in series["trials"]:
            model = load_ranker(checked_file(trial["checkpoint"]), protocol.protocol_id,
                                features, source_config["model"])
            rankings = predict(model, pool, features,
                               batch_size=source_config["evaluation_batch_size"])
            metrics = summarize(protocol, queries, rankings)
            if trial["seed"] == config["replay_seed"]:
                old = next(r for r in original["test_results"] if r["name"] == trial["name"])
                if metrics != old["metrics"]:
                    raise ValueError("seed 17 test metric replay differs")
            series["test_results"].append({
                "name": trial["name"], "seed": trial["seed"], "metrics": metrics,
                "trace": write_trace(output / f"test-{trial['name']}.jsonl.gz", queries, rankings),
            })
            del model
            save(series, output)
        if file_sha(source_file) != config["source_series_sha256"]:
            raise ValueError("original source changed during replication")
        series.update({"status": "completed", "finished_at": now(),
                       "peak_working_set_bytes": peak_memory_bytes()})
        save(series, output)
        archive = Path("docs/experiments/archive") / (output.name + ".json")
        if archive.exists():
            raise FileExistsError("archive already exists")
        archive.write_bytes((output / "series.json").read_bytes())
    except Exception as error:
        series.update({"status": "failed", "error": f"{type(error).__name__}: {error}"})
        save(series, output)
        raise
    return series


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("research/configs/r05-cold-replication.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    completed = run(args.config, args.output)
    print(json.dumps({"status": completed["status"], "replication_id": completed["replication_id"]}))
