"""Validation-only content-tower training and a separate static-content report."""
import argparse
import json
import math
import shutil
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from evorec.research.baselines import Popular, ItemCF
from evorec.research.content import ContentProtocol, ContentFeatures, ContentTower, ContentPredictor, reciprocal_fusion
from evorec.research.content_report import report
from evorec.research.neural import configure_seed
from evorec.research.protocol import summarize
from evorec.research.runner import file_sha, git_snapshot, peak_memory_bytes
from evorec.research.train import evaluate_baseline, write_trace
from evorec.research.training_baselines import RecentPopular, CollaborativeBlend


def now():
    return datetime.now(timezone.utc).isoformat()


def save(series, output):
    series["updated_at"] = now()
    temporary = output / "series.json.tmp"
    temporary.write_text(json.dumps(series, indent=2) + "\n")
    temporary.replace(output / "series.json")
    report(series, series["configuration"]["report_directory"])


def diagnostics(protocol, features, queries):
    histories = features.histories([q.history for q in queries], protocol.config["content"]["history_decay"])
    return {
        "all_positive_events": len(queries),
        "model_cold_available": sum(q.target_model_cold and q.target_available for q in queries),
        "target_unavailable": sum(not q.target_available for q in queries),
        "target_has_content_vector": int(sum(features.present[features.mapping[q.target]] for q in queries)),
        "queries_with_content_history": int((np.linalg.norm(histories, axis=1) > 1e-8).sum()),
        "cold_available_target_has_vector": int(sum(q.target_model_cold and q.target_available and features.present[features.mapping[q.target]] for q in queries)),
    }


def training_tensors(protocol, features):
    examples = protocol.training_examples()
    ids = protocol.vocabulary
    histories = features.histories([tuple(ids[i-1] for i in sequence) for sequence, _ in examples],
                                   protocol.config["content"]["history_decay"])
    train_items = [item for item in ids if features.present[features.mapping[item]]]
    labels = {item: i for i, item in enumerate(train_items)}
    positions = [i for i, (_, target) in enumerate(examples)
                 if ids[target-1] in labels and np.linalg.norm(histories[i]) > 1e-8]
    targets = torch.tensor([labels[ids[examples[i][1]-1]] for i in positions])
    return (
        torch.from_numpy(histories[positions]), targets,
        torch.from_numpy(features.vectors[[features.mapping[item] for item in train_items]]),
        {"raw_training_examples": len(examples), "usable_training_examples": len(positions),
         "skipped_missing_content_examples": len(examples)-len(positions), "training_candidate_items": len(train_items)},
    )


def load_tower(path, protocol, features, device):
    payload = torch.load(path, weights_only=True, map_location="cpu")
    if payload["protocol_id"] != protocol.protocol_id or payload["feature_shape"] != list(features.vectors.shape) or payload["feature_fingerprint"] != features.fingerprint:
        raise ValueError("content checkpoint protocol mismatch")
    model = ContentTower(features.vectors.shape[1], payload["setting"]["hidden"])
    model.load_state_dict(payload["state_dict"])
    return model.to(device)


def fit(protocol, features, fallback, validation, tensors, setting, seed, series, output, device):
    configure_seed(seed)
    torch.cuda.reset_peak_memory_stats()
    model = ContentTower(features.vectors.shape[1], setting["hidden"]).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=setting["learning_rate"], weight_decay=.01)
    histories, targets, candidates, _ = tensors
    candidates = candidates.to(device)
    loader = DataLoader(TensorDataset(histories, targets), batch_size=protocol.config["batch_size"],
                        shuffle=True, generator=torch.Generator().manual_seed(seed), num_workers=0)
    predictor = ContentPredictor(features, protocol.catalog, fallback, model, device,
                                 protocol.config["evaluation_batch_size"], protocol.config["content"]["history_decay"])
    trial = {"name": f"{setting['name']}-s{seed}", "seed": seed, "setting": setting,
             "status": "training", "history": [], "best_validation": None}
    series["trials"].append(trial)
    directory = output / trial["name"]
    directory.mkdir()
    checkpoint = directory / "best.pt"
    best, stale = -1.0, 0
    started = time.perf_counter()
    for epoch in range(1, protocol.config["max_epochs"]+1):
        epoch_started = time.perf_counter()
        model.train()
        loss_sum, n = 0.0, 0
        for values, labels in loader:
            optimizer.zero_grad(set_to_none=True)
            loss = nn.functional.cross_entropy(model(values.to(device), candidates, setting["temperature"]), labels.to(device))
            if not torch.isfinite(loss):
                raise ValueError("non-finite loss")
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0, error_if_nonfinite=True)
            optimizer.step()
            loss_sum += loss.item()*len(labels)
            n += len(labels)
        torch.cuda.synchronize()
        evaluation_started = time.perf_counter()
        measured = summarize(protocol, validation, predictor.rank_many(validation))
        metrics = measured["cohorts"]["all_positive_events"]
        row = {"epoch": epoch, "train_loss": loss_sum/n, "validation_ndcg@10": metrics["ndcg@10"],
               "validation_cold_recall@20": measured["cohorts"]["model_cold_available"]["recall@20"],
               "epoch_wall_seconds": time.perf_counter()-epoch_started,
               "validation_seconds": time.perf_counter()-evaluation_started}
        trial["history"].append(row)
        if metrics["ndcg@10"] > best:
            best, stale = metrics["ndcg@10"], 0
            trial.update({"best_epoch": epoch, "best_validation": measured})
            payload = {"protocol_id": protocol.protocol_id, "feature_shape": list(features.vectors.shape), "feature_fingerprint": features.fingerprint,
                       "setting": setting, "seed": seed, "epoch": epoch,
                       "state_dict": {k: v.detach().cpu() for k, v in model.state_dict().items()}}
            temporary = checkpoint.with_suffix(".tmp")
            torch.save(payload, temporary)
            temporary.replace(checkpoint)
        else:
            stale += 1
        save(series, output)
        print(json.dumps({"trial": trial["name"], **row, "best_epoch": trial["best_epoch"]}), flush=True)
        if stale >= protocol.config["patience"]:
            break
    trial.update({"status": "completed", "wall_seconds": time.perf_counter()-started,
                  "checkpoint": {"path_from_project_root": checkpoint.as_posix(), "sha256": file_sha(checkpoint)},
                  "gpu_trial_peak_allocated_bytes": torch.cuda.max_memory_allocated()})
    save(series, output)
    del model, optimizer, predictor
    torch.cuda.empty_cache()
    return trial


def run(config_path, output):
    config = json.loads(config_path.read_text())
    if config["stage"] != "R03-content":
        raise ValueError("unexpected stage")
    configure_seed(config["seeds"][0])
    if not torch.cuda.is_available():
        raise RuntimeError("this series requires CUDA")
    device = "cuda"
    protocol = ContentProtocol(config)
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    source.mkdir()
    for path in Path(__file__).parent.glob("*.py"):
        shutil.copyfile(path, source/path.name)
    shutil.copyfile(config_path, output/"configuration.json")
    series = {"status": "preparing", "started_at": now(), "protocol_id": protocol.protocol_id,
              "protocol": protocol.fingerprint, "configuration": config,
              "data_provenance": protocol.manifest, "metadata_provenance": protocol.metadata_manifest,
              "device": torch.cuda.get_device_name(), "torch": torch.__version__,
              "code": {**git_snapshot(), "source_sha256": {p.name: file_sha(p) for p in source.glob("*.py")}},
              "baselines": [], "trials": [], "test_results": []}
    try:
        features, feature_manifest = ContentFeatures.fit(protocol, output/"content-encoder")
        series["content_encoder"] = feature_manifest
        validation = protocol.queries("validation")
        series["validation_diagnostics"] = diagnostics(protocol, features, validation)
        tensors = training_tensors(protocol, features)
        series.update(tensors[3])
        if not len(tensors[1]) or not validation:
            raise ValueError("empty training or validation split")
        print(json.dumps({"phase": "features_ready", "encoder": feature_manifest, "examples": tensors[3],
                          "validation": series["validation_diagnostics"]}), flush=True)
        popular = Popular().fit(protocol.train, config["positive_rating_min"])
        popular.name = "Popular"
        prior = RecentPopular(365).fit(protocol.train, config["train_end_ms"], config["positive_rating_min"])
        core = ItemCF(max_user_items=100, neighbors=100).fit(protocol.train, config["positive_rating_min"])
        blend = CollaborativeBlend(core, prior, .25)
        statistical = [popular, prior, blend]
        base_validation = {}
        for model in statistical:
            metrics, rankings = evaluate_baseline(protocol, model, validation)
            base_validation[model.name] = rankings
            series["baselines"].append({"name": model.name, "metrics": metrics})
            print(json.dumps({"method": model.name, "validation": metrics["cohorts"]["all_positive_events"]}), flush=True)
            save(series, output)
        predictor = ContentPredictor(features, protocol.catalog, prior, device=device,
                                     batch_size=config["evaluation_batch_size"], decay=config["content"]["history_decay"])
        content_validation = predictor.rank_many(validation)
        series["baselines"].append({"name": "Content-SVD", "metrics": summarize(protocol, validation, content_validation)})
        for alpha in config["fusion_alphas"]:
            fused = reciprocal_fusion(base_validation[blend.name], content_validation, alpha, config["rrf_constant"])
            series["baselines"].append({"name": f"Content-RRF-a{alpha}", "metrics": summarize(protocol, validation, fused)})
        save(series, output)
        del predictor
        series["status"] = "training"
        choices = [fit(protocol, features, prior, validation, tensors, setting, config["seeds"][0], series, output, device)
                   for setting in config["trials"]]
        best = max(choices, key=lambda t: t["best_validation"]["cohorts"]["all_positive_events"]["ndcg@10"])
        series["selected_setting"] = best["setting"]
        series["setting_selected_at"] = now()
        for seed in config["seeds"][1:]:
            fit(protocol, features, prior, validation, tensors, best["setting"], seed, series, output, device)
        selected = [t for t in series["trials"] if t["setting"] == best["setting"]]
        model = load_tower(Path(best["checkpoint"]["path_from_project_root"]), protocol, features, device)
        predictor = ContentPredictor(features, protocol.catalog, prior, model, device, config["evaluation_batch_size"], config["content"]["history_decay"])
        neural_validation = predictor.rank_many(validation)
        fusion_choices = []
        for alpha in config["fusion_alphas"]:
            measured = summarize(protocol, validation, reciprocal_fusion(base_validation[blend.name], neural_validation, alpha, config["rrf_constant"]))
            fusion_choices.append({"alpha": alpha, "metrics": measured})
            series["baselines"].append({"name": f"Tower-RRF-a{alpha}-s{best['seed']}", "metrics": measured, "source": best["name"]})
        winner_fusion = max(fusion_choices, key=lambda r: r["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"])
        series["selected_fusion_alpha"] = winner_fusion["alpha"]
        candidates = [(r["name"], r["metrics"]) for r in series["baselines"]]
        candidates += [(t["name"], t["best_validation"]) for t in choices if t == best]
        series["selected_validation_method"] = max(candidates, key=lambda r: r[1]["cohorts"]["all_positive_events"]["ndcg@10"])[0]
        series["selection_finished_at"] = now()
        del predictor, model
        torch.cuda.empty_cache()
        series["status"] = "final_test"
        series["test_opened_at"] = now()
        save(series, output)
        test = protocol.queries("test", test_authorized=True)
        series["test_queries"] = len(test)
        series["test_diagnostics"] = diagnostics(protocol, features, test)
        base_test = {}
        def record(name, rankings, **extra):
            series["test_results"].append({"name": name, **extra, "metrics": summarize(protocol, test, rankings),
                                          "trace": write_trace(output/f"test-{name}.jsonl.gz", test, rankings)})
        for baseline in statistical:
            _, rankings = evaluate_baseline(protocol, baseline, test)
            base_test[baseline.name] = rankings
            record(baseline.name, rankings)
        predictor = ContentPredictor(features, protocol.catalog, prior, device=device, batch_size=config["evaluation_batch_size"], decay=config["content"]["history_decay"])
        raw = predictor.rank_many(test)
        record("Content-SVD", raw)
        for alpha in config["fusion_alphas"]:
            record(f"Content-RRF-a{alpha}", reciprocal_fusion(base_test[blend.name], raw, alpha, config["rrf_constant"]))
        del predictor
        for trial in selected:
            path = Path(trial["checkpoint"]["path_from_project_root"])
            if file_sha(path) != trial["checkpoint"]["sha256"]:
                raise ValueError("checkpoint integrity failure")
            model = load_tower(path, protocol, features, device)
            predictor = ContentPredictor(features, protocol.catalog, prior, model, device, config["evaluation_batch_size"], config["content"]["history_decay"])
            validation_again = summarize(protocol, validation, predictor.rank_many(validation))
            if validation_again != trial["best_validation"]:
                raise ValueError("reloaded content checkpoint metrics differ")
            neural = predictor.rank_many(test)
            record(trial["name"], neural, seed=trial["seed"], family="Content-Tower", checkpoint_reload_verified=True)
            alpha = series["selected_fusion_alpha"]
            record(f"Tower-RRF-a{alpha}-s{trial['seed']}",
                   reciprocal_fusion(base_test[blend.name], neural, alpha, config["rrf_constant"]),
                   seed=trial["seed"], family="Tower-RRF", checkpoint_reload_verified=True)
            del predictor, model
            torch.cuda.empty_cache()
        series["seed_summaries"] = {}
        for family in ("Content-Tower", "Tower-RRF"):
            members = [r for r in series["test_results"] if r.get("family") == family]
            keys = {"ndcg@10": ("all_positive_events", "ndcg@10"), "recall@20": ("all_positive_events", "recall@20"),
                    "candidate_recall@200": ("all_positive_events", "candidate_recall@200"),
                    "cold_recall@20": ("model_cold_available", "recall@20"),
                    "cold_candidate_recall@200": ("model_cold_available", "candidate_recall@200")}
            series["seed_summaries"][family] = {}
            for key, (cohort, metric) in keys.items():
                values = [r["metrics"]["cohorts"][cohort][metric] for r in members]
                series["seed_summaries"][family][key] = {"mean": statistics.mean(values), "sample_std": statistics.stdev(values),
                                                       "seeds": [r["seed"] for r in members]}
        series.update({"status": "completed", "finished_at": now(), "peak_working_set_bytes": peak_memory_bytes()})
        save(series, output)
    except Exception as error:
        series.update({"status": "failed", "error": f"{type(error).__name__}: {error}"})
        save(series, output)
        raise
    return series


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("research/configs/r03-content.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    series = run(args.config, args.output)
    print(json.dumps({"status": series["status"], "selected": series["selected_validation_method"],
                      "seed_summaries": series["seed_summaries"]}), flush=True)


if __name__ == "__main__":
    main()
