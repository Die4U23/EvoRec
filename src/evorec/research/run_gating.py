"""Frozen R03 models, new R04 users, validation-only cold reservation selection."""
import argparse
import gzip
import hashlib
import json
import shutil
import statistics
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch

from evorec.research.baselines import Popular, ItemCF
from evorec.research.content import ContentProtocol, ContentFeatures, ContentPredictor, reciprocal_fusion
from evorec.research.gating import history_signal, apply_policy, select_policy
from evorec.research.neural import configure_seed
from evorec.research.protocol import summarize
from evorec.research.runner import clean_experiment_snapshot, file_sha, peak_memory_bytes
from evorec.research.train import evaluate_baseline, write_trace
from evorec.research.train_content import load_tower, diagnostics
from evorec.research.training_baselines import RecentPopular, CollaborativeBlend


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(series, output):
    temporary = output / "series.json.tmp"
    temporary.write_text(json.dumps(series, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output / "series.json")


class FrozenPolicyProtocol(ContentProtocol):
    def __init__(self, config, parent, frozen_sha):
        super().__init__(config)
        for key in ("train_end_ms", "validation_end_ms", "positive_rating_min", "history_limit", "content"):
            if config[key] != parent.config[key]:
                raise ValueError(f"frozen protocol changed: {key}")
        if self.catalog != parent.catalog:
            raise ValueError("frozen catalog differs")
        if {e.user_id for e in self.events} & {e.user_id for e in parent.events}:
            raise ValueError("R03/R04 user overlap")
        # Query history stays in self.events; every fitted statistic and cold label uses R03.
        self.train, self.train_items, self.vocabulary = parent.train, parent.train_items, parent.vocabulary
        self.fingerprint.update({
            "protocol": "r04-new-users-frozen-r03-models-v1",
            "model_training_sample_sha256": parent.manifest["sample_sha256"],
            "frozen_series_sha256": frozen_sha,
            "gate": config["gate"], "policies": config["policies"], "selection": config["selection"],
            "fusion_alpha": config["fusion_alpha"], "rrf_constant": config["rrf_constant"],
        })
        self.protocol_id = hashlib.sha256(json.dumps(self.fingerprint, sort_keys=True).encode()).hexdigest()[:16]


def run(config_path, output):
    config = read(config_path)
    provenance = clean_experiment_snapshot()
    if config["stage"] != "R04-gating" or config["selection"]["seed"] != config["seeds"][0]:
        raise ValueError("unexpected experiment")
    frozen_dir = Path(config["frozen_run"])
    frozen = read(frozen_dir / "series.json")
    if frozen["status"] != "completed":
        raise ValueError("incomplete frozen run")
    # Do not silently change the algorithms used to reconstruct frozen model outputs.
    checked_sources = {}
    for name in ("data.py", "protocol.py", "content.py", "baselines.py", "training_baselines.py", "neural.py"):
        actual = file_sha(Path(__file__).parent / name)
        if actual != frozen["code"]["source_sha256"][name]:
            raise ValueError(f"frozen inference dependency changed: {name}")
        checked_sources[name] = actual
    parent = ContentProtocol(frozen["configuration"])
    if parent.protocol_id != frozen["protocol_id"]:
        raise ValueError("parent protocol drift")
    frozen_sha = file_sha(frozen_dir / "series.json")
    protocol = FrozenPolicyProtocol(config, parent, frozen_sha)
    encoder_dir = frozen_dir / "content-encoder"
    for name, expected in frozen["content_encoder"]["files"].items():
        if file_sha(encoder_dir / name) != expected:
            raise ValueError(f"encoder integrity failure: {name}")
    features = ContentFeatures(read(encoder_dir / "items.json"), np.load(encoder_dir / "vectors.npy"))
    trials = [t for t in frozen["trials"] if t["setting"] == frozen["selected_setting"]]
    if [t["seed"] for t in trials] != config["seeds"]:
        raise ValueError("frozen seed selection mismatch")
    for trial in trials:
        if file_sha(Path(trial["checkpoint"]["path_from_project_root"])) != trial["checkpoint"]["sha256"]:
            raise ValueError("checkpoint integrity failure")
    configure_seed(config["seeds"][0])
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA required for the registered run")
    output.mkdir(parents=True, exist_ok=False)
    (output / "source").mkdir()
    for path in Path(__file__).parent.glob("*.py"):
        shutil.copyfile(path, output / "source" / path.name)
    shutil.copyfile(config_path, output / "configuration.json")
    series = {
        "status": "validation", "started_at": now(), "protocol_id": protocol.protocol_id,
        "protocol": protocol.fingerprint, "configuration": config,
        "data_provenance": protocol.manifest, "metadata_provenance": protocol.metadata_manifest,
        "frozen": {"series_sha256": frozen_sha, "protocol_id": parent.protocol_id,
                   "training_data_provenance": parent.manifest, "trials": trials,
                   "encoder": frozen["content_encoder"], "feature_fingerprint": features.fingerprint,
                   "inference_source_sha256": checked_sources},
        "device": torch.cuda.get_device_name(), "torch": torch.__version__,
        "code": {**provenance, "source_sha256": {p.name: file_sha(p) for p in (output/"source").glob("*.py")}},
        "validation_results": [], "test_results": [],
    }
    save(series, output)
    try:
        popular = Popular().fit(parent.train, config["positive_rating_min"])
        prior = RecentPopular(365).fit(parent.train, config["train_end_ms"], config["positive_rating_min"])
        core = ItemCF(max_user_items=100, neighbors=100).fit(parent.train, config["positive_rating_min"])
        blend = CollaborativeBlend(core, prior, .25)
        def prepare(queries, split):
            signals = [history_signal(features, q.history, config["content"]["history_decay"]) for q in queries]
            _, collaborative = evaluate_baseline(protocol, blend, queries)
            raw = ContentPredictor(features, protocol.catalog, prior, device="cuda",
                                   batch_size=config["evaluation_batch_size"],
                                   decay=config["content"]["history_decay"]).rank_many(queries)
            series[split+"_diagnostics"] = diagnostics(protocol, features, queries)
            print(json.dumps({"phase": split+"_candidates_ready", **series[split+"_diagnostics"]}), flush=True)
            return signals, collaborative, raw

        def neural(queries, trial):
            model = load_tower(Path(trial["checkpoint"]["path_from_project_root"]), parent, features, "cuda")
            predictor = ContentPredictor(features, protocol.catalog, prior, model, "cuda",
                                         config["evaluation_batch_size"], config["content"]["history_decay"])
            result = predictor.rank_many(queries)
            del predictor, model
            torch.cuda.empty_cache()
            return result

        def record(split, queries, name, rankings, **extra):
            row = {"name": name, **extra, "metrics": summarize(protocol, queries, rankings),
                   "trace": write_trace(output/f"{split}-{name}.jsonl.gz", queries, rankings)}
            series[split+"_results"].append(row)
            save(series, output)
            print(json.dumps({"phase": split, "method": name,
                              "ndcg": row["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"],
                              "cold_recall": row["metrics"]["cohorts"]["model_cold_available"]["recall@20"]}), flush=True)
            return row

        def policies(split, queries, signals, raw, fixed, seed, choices):
            for policy in choices:
                rankings, gate_records = apply_policy(fixed, raw, signals, parent.train_items, policy, config["gate"])
                name = f"{policy['name']}-s{seed}"
                gate_path = output/f"{split}-{name}-gate.jsonl.gz"
                with gzip.open(gate_path, "wt", encoding="utf-8") as stream:
                    for query, entry in zip(queries, gate_records, strict=True):
                        stream.write(json.dumps({"query_id": query.query_id, **entry})+"\n")
                stats = {"requests": len(queries),
                         "gate_passed": sum(r["gate_passed"] for r in gate_records),
                         "ranking_changed": sum(r["ranking_changed"] for r in gate_records),
                         "new_cold_promoted": sum(r["new_cold_promoted"] for r in gate_records)}
                record(split, queries, name, rankings, seed=seed, policy=policy,
                       gate_statistics=stats, gate_trace={"path_from_project_root": gate_path.as_posix(),
                                                         "sha256": file_sha(gate_path)})
        validation = protocol.queries("validation")
        signals, collaborative, raw = prepare(validation, "validation")
        record("validation", validation, "Content-SVD", raw)
        fixed = reciprocal_fusion(collaborative, neural(validation, trials[0]),
                                  config["fusion_alpha"], config["rrf_constant"])
        policies("validation", validation, signals, raw, fixed, trials[0]["seed"], config["policies"])
        selected, floor = select_policy([r for r in series["validation_results"] if "policy" in r],
                                        config["selection"]["ndcg_min_ratio"])
        series.update({"selected_policy": selected, "validation_ndcg_floor": floor,
                       "selection_finished_at": now()})
        save(series, output)
        del validation, signals, collaborative, raw, fixed
        series.update({"status": "final_test", "test_opened_at": now()})
        save(series, output)
        test = protocol.queries("test", test_authorized=True)
        series["test_queries"] = len(test)
        signals, collaborative, raw = prepare(test, "test")
        _, popular_ranking = evaluate_baseline(protocol, popular, test)
        record("test", test, "Popular", popular_ranking)
        del popular_ranking
        record("test", test, "CF-blend-a0.25", collaborative)
        record("test", test, "Content-SVD", raw)
        for trial in trials:
            fixed = reciprocal_fusion(collaborative, neural(test, trial), config["fusion_alpha"], config["rrf_constant"])
            choices = config["policies"] if trial["seed"] == config["seeds"][0] else [
                p for p in config["policies"] if p["name"] in {"fixed", selected["name"]}]
            policies("test", test, signals, raw, fixed, trial["seed"], choices)
            del fixed
        series["seed_summaries"] = {}
        for name in dict.fromkeys(("fixed", selected["name"])):
            members = [r for r in series["test_results"] if r.get("policy", {}).get("name") == name]
            summary = {}
            for key in ("ndcg@10", "recall@20", "candidate_recall@200", "cold_recall@20", "cold_candidate_recall@200"):
                cohort = "model_cold_available" if key.startswith("cold_") else "all_positive_events"
                values = [r["metrics"]["cohorts"][cohort][key.removeprefix("cold_")] for r in members]
                summary[key] = {"mean": statistics.mean(values), "sample_std": statistics.stdev(values),
                                "seeds": [r["seed"] for r in members]}
            series["seed_summaries"][name] = summary
        series.update({"status": "completed", "finished_at": now(), "peak_working_set_bytes": peak_memory_bytes()})
        save(series, output)
    except Exception as error:
        series.update({"status": "failed", "error": f"{type(error).__name__}: {error}"})
        save(series, output)
        raise
    return series


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("research/configs/r04-gating.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.config, args.output)
    print(json.dumps({"status": result["status"], "selected_policy": result["selected_policy"],
                      "seed_summaries": result["seed_summaries"]}), flush=True)
