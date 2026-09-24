"""Bounded validation-only training, automatic reports, and a final sealed test evaluation."""

import os
os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")

import argparse
import gzip
import json
import math
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from evorec.research.baselines import ItemCF, Popular
from evorec.research.neural import NeuralPredictor, SequenceModel, configure_seed, padded
from evorec.research.protocol import AvailableAt, Protocol, summarize, trace_records
from evorec.research.reporting import update_report
from evorec.research.runner import clean_experiment_snapshot, file_sha, peak_memory_bytes
from evorec.research.training_baselines import CollaborativeBlend, RecentPopular


def now():
    return datetime.now(timezone.utc).isoformat()


def save_state(series, directory):
    series["updated_at"] = now()
    temporary = directory / "series.json.tmp"
    temporary.write_text(json.dumps(series, indent=2) + "\n", encoding="utf-8")
    temporary.replace(directory / "series.json")
    update_report(series)


def evaluate_baseline(protocol, model, queries):
    started = time.perf_counter()
    rankings = [model.rank(query.history, query.seen, AvailableAt(protocol.catalog, query.timestamp_ms), 200) for query in queries]
    result = summarize(protocol, queries, rankings)
    result["evaluation_wall_seconds"] = time.perf_counter() - started
    return result, rankings


def write_trace(path, queries, rankings):
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for record in trace_records(queries, rankings):
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
    return {"path_from_project_root": path.as_posix(), "sha256": file_sha(path)}


def load_model(checkpoint, protocol, device):
    payload = torch.load(checkpoint, map_location="cpu", weights_only=True)
    if payload["protocol_id"] != protocol.protocol_id or tuple(payload["vocabulary"]) != protocol.vocabulary:
        raise ValueError("checkpoint belongs to another protocol or vocabulary")
    model = SequenceModel(len(protocol.vocabulary), protocol.config["history_limit"], **payload["architecture"])
    model.load_state_dict(payload["state_dict"])
    return model.to(device)


def fit_trial(protocol, popular, validation, tensors, setting, seed, series, output, device):
    configure_seed(seed)
    name = f"{setting['name']}-s{seed}"
    directory = output / name
    directory.mkdir()
    architecture = {key: setting[key] for key in ("hidden", "heads", "layers", "dropout")}
    model = SequenceModel(len(protocol.vocabulary), protocol.config["history_limit"], **architecture).to(device)
    with torch.no_grad():
        model.output_bias.copy_(torch.tensor(
            [math.log(popular.counts[item]) for item in protocol.vocabulary], device=device,
        ))
    optimizer = torch.optim.AdamW(model.parameters(), lr=setting["learning_rate"], weight_decay=.01)
    loader = DataLoader(
        TensorDataset(*tensors), batch_size=protocol.config["batch_size"], shuffle=True,
        generator=torch.Generator().manual_seed(seed), num_workers=0, pin_memory=device == "cuda",
    )
    predictor = NeuralPredictor(model, protocol.vocabulary, popular, protocol.catalog, device, protocol.config["evaluation_batch_size"])
    trial = {"name": name, "seed": seed, "setting": setting, "status": "training", "history": [], "best_validation": None}
    series["trials"].append(trial)
    best, stale = -1.0, 0
    checkpoint = directory / "best.pt"
    started = time.perf_counter()
    for epoch in range(1, protocol.config["max_epochs"] + 1):
        epoch_started = time.perf_counter()
        model.train()
        total_loss, examples = 0.0, 0
        for sequences, lengths, targets in loader:
            sequences, lengths, targets = sequences.to(device), lengths.to(device), targets.to(device)
            optimizer.zero_grad(set_to_none=True)
            loss = nn.functional.cross_entropy(model(sequences, lengths), targets - 1)
            if not torch.isfinite(loss):
                raise ValueError("non-finite training loss")
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0, error_if_nonfinite=True)
            optimizer.step()
            total_loss += loss.item() * len(targets)
            examples += len(targets)
        if device == "cuda":
            torch.cuda.synchronize()
        validation_started = time.perf_counter()
        rankings = predictor.rank_many(validation)
        measured = summarize(protocol, validation, rankings)
        metrics = measured["cohorts"]["all_positive_events"]
        row = {
            "epoch": epoch, "train_loss": total_loss / examples,
            "validation_ndcg@10": metrics["ndcg@10"], "validation_recall@20": metrics["recall@20"],
            "wall_seconds": time.perf_counter() - epoch_started,
            "validation_seconds": time.perf_counter() - validation_started,
        }
        trial["history"].append(row)
        if metrics["ndcg@10"] > best:
            best, stale = metrics["ndcg@10"], 0
            trial["best_epoch"], trial["best_validation"] = epoch, measured
            payload = {
                "protocol_id": protocol.protocol_id, "vocabulary": protocol.vocabulary,
                "architecture": architecture, "setting": setting, "seed": seed, "epoch": epoch,
                "state_dict": {key: value.detach().cpu() for key, value in model.state_dict().items()},
            }
            temporary = checkpoint.with_suffix(".tmp")
            torch.save(payload, temporary)
            temporary.replace(checkpoint)
        else:
            stale += 1
        save_state(series, output)
        print(json.dumps({"trial": name, **row, "best_epoch": trial.get("best_epoch")}), flush=True)
        if stale >= protocol.config["patience"]:
            break
    trial.update({
        "status": "completed", "training_wall_seconds": time.perf_counter() - started,
        "checkpoint": {"path_from_project_root": checkpoint.as_posix(), "sha256": file_sha(checkpoint)},
        "gpu_peak_allocated_bytes": torch.cuda.max_memory_allocated() if device == "cuda" else None,
    })
    save_state(series, output)
    del predictor, optimizer, model
    if device == "cuda":
        torch.cuda.empty_cache()
    return trial


def run(config_path, output):
    config = json.loads(config_path.read_text())
    if config["stage"] != "R02-R03-training":
        raise ValueError("unexpected training stage")
    provenance = clean_experiment_snapshot()
    protocol = Protocol(config)
    output.mkdir(parents=True, exist_ok=False)
    source_dir = output / "source"
    source_dir.mkdir()
    for path in Path(__file__).parent.glob("*.py"):
        shutil.copyfile(path, source_dir / path.name)
    shutil.copyfile(config_path, output / "configuration.json")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device != "cuda":
        raise RuntimeError("this recorded series requires the validated GPU environment")
    series = {
        "schema_version": "0.2", "status": "preparing", "started_at": now(),
        "protocol_id": protocol.protocol_id, "protocol": protocol.fingerprint,
        "configuration": config, "data_provenance": protocol.manifest, "statistics": protocol.statistics,
        "device": torch.cuda.get_device_name(0), "torch": torch.__version__, "torch_cuda": torch.version.cuda,
        "environment_note": "research venv inherits existing system packages; service venv is unchanged",
        "code": {**provenance, "source_sha256": {p.name: file_sha(p) for p in sorted(source_dir.glob("*.py"))}},
        "baselines": [], "trials": [], "test_results": None,
    }
    try:
        validation = protocol.queries("validation")
        if not validation:
            raise ValueError("no validation queries")
        examples = protocol.training_examples()
        sequences, lengths = padded([history for history, _ in examples], config["history_limit"])
        targets = torch.tensor([target for _, target in examples], dtype=torch.long)
        tensors = (sequences, lengths, targets)
        series["training_examples"] = len(examples)
        series["validation_queries"] = len(validation)
        series["vocabulary_items"] = len(protocol.vocabulary)
        print(json.dumps({"training_examples": len(examples), "validation_queries": len(validation), "vocabulary_items": len(protocol.vocabulary)}), flush=True)
        popular = Popular().fit(protocol.train, config["positive_rating_min"])
        popular.name = "Popular"
        recent = RecentPopular(365).fit(protocol.train, config["train_end_ms"], config["positive_rating_min"])
        core = ItemCF(max_user_items=100, neighbors=100).fit(protocol.train, config["positive_rating_min"])
        core.name = "ItemCF"
        baselines = [popular, recent, core, CollaborativeBlend(core, recent, .25), CollaborativeBlend(core, recent, .5)]
        for baseline in baselines:
            measured, _ = evaluate_baseline(protocol, baseline, validation)
            series["baselines"].append({"name": baseline.name, "validation": measured})
            save_state(series, output)
            print(json.dumps({"baseline": baseline.name, "validation": measured["cohorts"]["all_positive_events"]}), flush=True)
        series["status"] = "training"
        selection_trials = [
            fit_trial(protocol, popular, validation, tensors, setting, config["seeds"][0], series, output, device)
            for setting in config["trials"]
        ]
        best = max(selection_trials, key=lambda trial: trial["best_validation"]["cohorts"]["all_positive_events"]["ndcg@10"])
        series["selected_setting"] = best["setting"]
        series["selection_finished_at"] = now()
        for seed in config["seeds"][1:]:
            fit_trial(protocol, popular, validation, tensors, best["setting"], seed, series, output, device)
        selected = [trial for trial in series["trials"] if trial["setting"] == best["setting"]]
        # Test access is deliberate and occurs only after every selected model has finished.
        series["status"] = "final_test"
        series["test_opened_at"] = now()
        save_state(series, output)
        test = protocol.queries("test", test_authorized=True)
        series["test_queries"] = len(test)
        series["test_results"] = []
        for baseline in baselines:
            measured, rankings = evaluate_baseline(protocol, baseline, test)
            series["test_results"].append({
                "name": baseline.name, "metrics": measured,
                "trace": write_trace(output / f"test-{baseline.name}.jsonl.gz", test, rankings),
            })
        for trial in selected:
            checkpoint = Path(trial["checkpoint"]["path_from_project_root"])
            if file_sha(checkpoint) != trial["checkpoint"]["sha256"]:
                raise ValueError("checkpoint checksum mismatch")
            model = load_model(checkpoint, protocol, device)
            predictor = NeuralPredictor(model, protocol.vocabulary, popular, protocol.catalog, device, config["evaluation_batch_size"])
            validation_again = summarize(protocol, validation, predictor.rank_many(validation))
            for key in ("ndcg@10", "recall@20", "candidate_recall@200"):
                if not math.isclose(
                    validation_again["cohorts"]["all_positive_events"][key],
                    trial["best_validation"]["cohorts"]["all_positive_events"][key], abs_tol=1e-12,
                ):
                    raise ValueError("reloaded checkpoint differs from selected validation metrics")
            rankings = predictor.rank_many(test)
            series["test_results"].append({
                "name": trial["name"], "seed": trial["seed"], "metrics": summarize(protocol, test, rankings),
                "checkpoint_reload_verified": True,
                "trace": write_trace(output / f"test-{trial['name']}.jsonl.gz", test, rankings),
            })
            del model, predictor
            torch.cuda.empty_cache()
        neural_results = [row for row in series["test_results"] if "seed" in row]
        series["selected_neural_seed_summary"] = {
            key: {
                "mean": float(np.mean([row["metrics"]["cohorts"]["all_positive_events"][key] for row in neural_results])),
                "sample_std": float(np.std([row["metrics"]["cohorts"]["all_positive_events"][key] for row in neural_results], ddof=1)),
                "seeds": [row["seed"] for row in neural_results],
            } for key in ("ndcg@10", "recall@20", "candidate_recall@200")
        }
        series.update({"status": "completed", "finished_at": now(), "peak_working_set_bytes": peak_memory_bytes()})
        save_state(series, output)
    except Exception as error:
        series.update({"status": "failed", "error": f"{type(error).__name__}: {error}"})
        save_state(series, output)
        raise
    return series


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("research/configs/r02-training.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.config, args.output)
    print(json.dumps({"status": result["status"], "selected_setting": result["selected_setting"], "test_results": result["selected_neural_seed_summary"]}), flush=True)


if __name__ == "__main__":
    main()
