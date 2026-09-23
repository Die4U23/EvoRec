"""Run one recorded CPU feasibility experiment; no network or public API mutations."""

import argparse
import gzip
import hashlib
import json
import os
import platform
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from evorec.research.baselines import ItemCF, Popular
from evorec.research.data import load_events
from evorec.research.evaluation import evaluate


EXPERIMENT_SCOPES = ("src/evorec/research", "research/configs", "tests", "scripts")


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git_snapshot():
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL))
        return {"git_base_commit": commit, "working_tree_dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"git_base_commit": None, "working_tree_dirty": None}


def clean_experiment_snapshot(scopes=EXPERIMENT_SCOPES):
    """Return Git provenance, rejecting uncommitted experiment inputs.

    Ignored datasets and run outputs remain allowed. Changes outside the declared
    experiment scopes are recorded, but cannot silently alter the experiment.
    """
    scopes = tuple(str(path).replace("\\", "/") for path in scopes)
    snapshot = git_snapshot()
    if snapshot["git_base_commit"] is None:
        raise RuntimeError("recorded experiments require a Git checkout")
    try:
        scoped_changes = subprocess.check_output(
            ["git", "-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=all", "--", *scopes],
            text=True,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
        ).splitlines()
        all_changes = subprocess.check_output(
            ["git", "-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=all"],
            text=True,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
        ).splitlines()
        tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("unable to verify experiment Git provenance") from error
    if scoped_changes:
        changed = ", ".join(scoped_changes[:8])
        suffix = " ..." if len(scoped_changes) > 8 else ""
        raise RuntimeError(f"commit experiment code, configuration, tests and scripts before running: {changed}{suffix}")
    return {
        **snapshot,
        "git_tree": tree,
        "experiment_paths_clean": True,
        "experiment_scopes": list(scopes),
        "unrelated_worktree_changes": [line for line in all_changes if line not in scoped_changes],
    }


def peak_memory_bytes():
    if platform.system() == "Windows":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                )
            ]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize
        return None
    if platform.system() == "Linux":
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    return None


def run(config_path: Path, output: Path):
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config["stage"] != "R01-feasibility":
        raise ValueError("this runner only implements R01 feasibility")
    positive_min = config["positive_rating_min"]
    if not isinstance(positive_min, (int, float)) or not 1 <= positive_min <= 5:
        raise ValueError("invalid positive rating threshold")
    if config["train_end_ms"] >= config["validation_end_ms"]:
        raise ValueError("invalid time boundaries")
    dataset = Path(config["dataset_path"])
    data_provenance = json.loads(dataset.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    if data_provenance["status"] != "completed_prefix_sample" or file_sha(dataset) != data_provenance["sample_sha256"]:
        raise ValueError("sample provenance or checksum mismatch")
    code_provenance = clean_experiment_snapshot()
    output.mkdir(parents=True, exist_ok=False)
    manifest_path = output / "run.json"
    manifest = {
        "schema_version": "0.1", "status": "running", "stage": "R01-feasibility",
        "started_at": datetime.now(timezone.utc).isoformat(), "configuration": config,
        "data_provenance": data_provenance,
        "environment": {
            "python": platform.python_version(), "os": platform.platform(),
            "logical_processors": os.cpu_count(), "compute": "CPU", "third_party_research_dependencies": [],
        },
        "code": {
            **code_provenance,
            "source_sha256": {p.name: file_sha(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
            "config_sha256": file_sha(config_path),
        },
        "protocol": {
            "fit": "training positives only; models frozen through validation and test",
            "positive_event": "rating >= configured threshold; review is an implicit-interest proxy, not a click",
            "catalog": "items first observed strictly before request timestamp in this sample",
            "history": "earlier positives only, rolling window; all earlier reviews excluded from candidates",
            "timestamp_ties": "evaluate complete equal-timestamp batch before applying any event in it",
            "unavailable_targets": "retained as misses in all_positive_events; conditional cohort also reported",
            "model_cold": "no training-period interaction, regardless of rating",
            "itemcf": "binary cosine on capped positive user baskets, summed over recent positive history; popularity fills remaining candidates",
            "coverage": "top-10 unique recommendations divided by union of available catalog over evaluated request times",
            "latency": "rank function only, excludes file IO, HTTP, database and GPU; not an online SLA",
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    started, cpu_started = time.perf_counter(), time.process_time()
    try:
        events, statistics = load_events(dataset)
        if statistics["input_rows"] != data_provenance["rows"]:
            raise ValueError("manifest row count mismatch")
        train = [event for event in events if event.timestamp_ms < config["train_end_ms"]]
        if not any(event.rating >= positive_min for event in train):
            raise ValueError("no training positives")
        models = [Popular(), ItemCF(config["itemcf_max_user_items"], config["itemcf_neighbors"])]
        fits = {}
        for model in models:
            fit_started = time.perf_counter()
            model.fit(train, positive_min)
            fits[model.name] = {"wall_seconds": time.perf_counter() - fit_started, **model.fit_stats}
        eval_started = time.perf_counter()
        with gzip.open(output / "requests.jsonl.gz", "wt", encoding="utf-8") as request_output:
            result = evaluate(
                events, models, config["train_end_ms"], config["validation_end_ms"],
                config["history_limit"], config["candidate_k"], positive_min, request_output,
            )
        manifest.update({
            "status": "completed", "finished_at": datetime.now(timezone.utc).isoformat(),
            "data_statistics": statistics, "fits": fits, **result,
            "resources": {
                "load_fit_evaluate_wall_seconds": time.perf_counter() - started,
                "process_cpu_seconds": time.process_time() - cpu_started,
                "evaluation_wall_seconds_including_trace_io": time.perf_counter() - eval_started,
                "process_peak_working_set_bytes": peak_memory_bytes(),
            },
            "request_trace": {"path": "requests.jsonl.gz", "sha256": file_sha(output / "requests.jsonl.gz")},
            "limitations": [
                "biased prefix sample, not an official benchmark or representative population",
                "no metadata, dense retrieval, sequence model, generative model or GPU training",
                "review ratings are proxies; no claims about production clicks or CTR",
                "sample first observation is not item listing time",
                "no tuning or multiple training seeds; these baselines are deterministic",
                "ItemCF uses capped user baskets and capped neighbors",
                "current timing is a single-process offline measurement",
            ],
        })
    except Exception as error:
        manifest.update({"status": "failed", "error": f"{type(error).__name__}: {error}"})
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        raise
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("research/configs/r01-small.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.config, args.output)
    print(json.dumps({
        "status": result["status"], "statistics": result["data_statistics"],
        "diagnostics": result["diagnostics"], "resources": result["resources"],
        "metrics": {split: {method: groups["all_positive_events"] for method, groups in methods.items()}
                    for split, methods in result["metrics"].items()},
    }, indent=2))


if __name__ == "__main__":
    main()
