"""Check clean R02-R05 replications against the immutable original runs."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


COMMIT = "7186c874cfa9a6a7c4129704afb75e54fbee76a2"
RUNS = (
    ("R02", "r02-training-20260915", "r02-clean-replication-20260923", "r02-clean-replication-audit.json", ("selected_setting",), "selected_neural_seed_summary"),
    ("R03", "r03-content-20260915", "r03-verified-clean-replication-20260924", "r03-verified-clean-audit.json", ("selected_setting", "selected_fusion_alpha", "selected_validation_method"), "seed_summaries"),
    ("R04", "r04-gating-20260915", "r04-verified-clean-replication-20260924", "r04-verified-clean-audit.json", ("selected_policy",), "seed_summaries"),
    ("R05", "r05-ranker-20260916", "r05-verified-clean-replication-20260924", "r05-verified-clean-audit.json", ("selected_setting", "selected_method"), "seed_summary"),
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def quality_metrics(value):
    """Remove measured wall time, retaining every quality and coverage field."""
    if isinstance(value, dict):
        return {key: quality_metrics(item) for key, item in value.items()
                if key != "evaluation_wall_seconds"}
    if isinstance(value, list):
        return [quality_metrics(item) for item in value]
    return value


def check(root=Path(".")):
    results = []
    for stage, original_id, replica_id, audit_name, selection_keys, summary_key in RUNS:
        old_path = root / "artifacts" / "runs" / original_id / "series.json"
        new_path = root / "artifacts" / "runs" / replica_id / "series.json"
        audit_path = root / "tmp" / audit_name
        old, new, audit = read(old_path), read(new_path), read(audit_path)
        published_audit = root / "docs" / "validation" / audit_name
        if published_audit.exists() and read(published_audit) != audit:
            raise ValueError(f"{stage}: published audit differs from the local audit")
        if old["status"] != new["status"] or new["status"] != "completed":
            raise ValueError(f"{stage}: incomplete original or replication")
        if old["code"]["working_tree_dirty"] is not True:
            raise ValueError(f"{stage}: original dirty flag was rewritten")
        code = new["code"]
        if code["git_base_commit"] != COMMIT or code["working_tree_dirty"] is not False:
            raise ValueError(f"{stage}: replication did not start at the clean commit")
        if code["experiment_paths_clean"] is not True or audit["status"] != "passed":
            raise ValueError(f"{stage}: source gate or independent audit failed")
        if audit["series_sha256"] != digest(new_path) or audit["protocol_id"] != new["protocol_id"]:
            raise ValueError(f"{stage}: audit is not bound to the replication series")
        if new["configuration"] != old["configuration"]:
            raise ValueError(f"{stage}: registered configuration changed")
        if any(new[key] != old[key] for key in selection_keys) or new[summary_key] != old[summary_key]:
            raise ValueError(f"{stage}: selection or seed summary changed")
        old_methods = {row["name"]: quality_metrics(row["metrics"]) for row in old["test_results"]}
        new_methods = {row["name"]: quality_metrics(row["metrics"]) for row in new["test_results"]}
        if old_methods != new_methods:
            raise ValueError(f"{stage}: test quality or coverage metrics changed")
        old_epochs = sum(len(trial["history"]) for trial in old.get("trials", []))
        new_epochs = sum(len(trial["history"]) for trial in new.get("trials", []))
        if old_epochs != new_epochs:
            raise ValueError(f"{stage}: training epoch count changed")
        source_dir = root / "artifacts" / "runs" / replica_id / "source"
        for name, expected in code["source_sha256"].items():
            source = source_dir / name
            if digest(source) != expected:
                raise ValueError(f"{stage}: source snapshot hash changed: {name}")
            committed = subprocess.check_output(
                ["git", "show", f"{COMMIT}:src/evorec/research/{name}"], cwd=root,
            )
            if hashlib.sha256(committed).hexdigest() != expected:
                raise ValueError(f"{stage}: source differs from the clean Git commit: {name}")
        results.append({
            "stage": stage,
            "original_run": original_id,
            "replication_run": replica_id,
            "replication_series_sha256": digest(new_path),
            "independent_audit_json_sha256": digest_json(audit),
            "source_files_verified_against_commit": len(code["source_sha256"]),
            "training_epochs": new_epochs,
            "test_methods_with_identical_quality_metrics": len(new_methods),
            "selection_and_seed_summary_identical": True,
            "protocol_id_identical": old["protocol_id"] == new["protocol_id"],
            "working_tree_dirty_at_start": False,
            "experiment_paths_clean_at_start": True,
        })
    return {
        "status": "passed",
        "code_commit": COMMIT,
        "replications": results,
        "limits": [
            "Original runs retain working_tree_dirty=true; the clean replications are new runs.",
            "Original tests had already been inspected, so this is reproducibility evidence rather than a new sealed test.",
            "Dependency installation was reused from the local research environment; a fresh-machine rebuild remains unverified.",
            "Offline wall time and serialized container hashes may differ while quality metrics remain identical.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(check(args.root.resolve()), indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
