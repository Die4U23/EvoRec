"""Verify that the exact R02-R05 source snapshots remain reconstructable."""

import argparse
import base64
import hashlib
import json
import subprocess
from pathlib import Path


DEFAULT_MANIFEST = Path("research/provenance/r02-r05-source-reconstruction.json")
RUN_ROOT = Path("artifacts/runs")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def recover(record):
    if record["type"] == "git":
        object_name = f'{record["commit"]}:{record["path"]}'
        blob_oid = subprocess.check_output(["git", "rev-parse", object_name], text=True).strip()
        if blob_oid != record["git_blob_oid"]:
            raise ValueError(f"Git blob changed for {object_name}")
        data = subprocess.check_output(["git", "show", object_name])
    elif record["type"] == "vendored_base64":
        data = base64.b64decode(Path(record["path"]).read_text(encoding="ascii"))
    else:
        raise ValueError(f'unknown recovery type: {record["type"]}')
    if sha256(data) != record["sha256"]:
        raise ValueError(f'fingerprint mismatch for {record["path"]}')
    return data


def check(manifest_path=DEFAULT_MANIFEST):
    manifest = read_json(manifest_path)
    if manifest["status"] != "historical_source_reconstruction":
        raise ValueError("unexpected reconstruction manifest")
    results = []
    for run in manifest["runs"]:
        archive = read_json(Path(run["archive_path"]))
        if archive["code"]["git_base_commit"] != run["original_git_base_commit"]:
            raise ValueError(f'base commit mismatch for {run["run_id"]}')
        if archive["code"]["working_tree_dirty"] is not True or not run["original_working_tree_dirty"]:
            raise ValueError(f'historical dirty flag was altered for {run["run_id"]}')

        configuration = recover(run["configuration"])
        if json.loads(configuration) != archive["configuration"]:
            raise ValueError(f'configuration differs for {run["run_id"]}')

        source_hashes = {}
        recovery_counts = {"git": 0, "vendored_base64": 0}
        for source in run["source_files"]:
            data = recover(source["recovery"])
            digest = sha256(data)
            source_hashes[source["name"]] = digest
            recovery_counts[source["recovery"]["type"]] += 1
        if source_hashes != archive["code"]["source_sha256"]:
            raise ValueError(f'archive source map differs for {run["run_id"]}')
        tree_description = "".join(
            f'{source["name"]}\\0{source_hashes[source["name"]]}\\n'
            for source in run["source_files"]
        ).encode()
        if sha256(tree_description) != run["reconstructed_source_tree_sha256"]:
            raise ValueError(f'reconstructed source tree differs for {run["run_id"]}')

        local_run = RUN_ROOT / run["run_id"]
        local_verified = False
        if local_run.is_dir():
            if (local_run / "series.json").read_bytes() != Path(run["archive_path"]).read_bytes():
                raise ValueError(f'local series differs from archive for {run["run_id"]}')
            if (local_run / "configuration.json").read_bytes() != configuration:
                raise ValueError(f'local configuration differs for {run["run_id"]}')
            for source in run["source_files"]:
                path = local_run / "source" / source["name"]
                if sha256(path.read_bytes()) != source["recovery"]["sha256"]:
                    raise ValueError(f'local source differs: {path}')
            local_verified = True
        results.append({
            "run_id": run["run_id"],
            "source_files": len(source_hashes),
            "git_recovered": recovery_counts["git"],
            "vendored": recovery_counts["vendored_base64"],
            "local_run_verified": local_verified,
            "original_working_tree_dirty": True,
            "exact_source_reconstructable": True,
        })
    return {
        "status": "passed",
        "claim": manifest["claim"],
        "manifest_sha256": sha256(manifest_path.read_bytes()),
        "checker_sha256": sha256(Path(__file__).read_bytes()),
        "runs_checked": len(results),
        "runs": results,
        "limitations": [
            "The original dirty-worktree fact is preserved, not rewritten.",
            "Reconstruction proves exact source and configuration bytes, not the complete historical dependency installation.",
            "A clean replication is still required for a new clean-run claim.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = check(args.manifest)
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
