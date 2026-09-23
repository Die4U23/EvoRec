"""Check replication provenance, checkpoints, reports and published chart copies."""
import ast
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

import torch
from PIL import Image
from evorec.research.ranker import SCALAR_NAMES
from evorec.research.replicate_ranker import checkpoint_states_equal, checked_file
from evorec.research.runner import file_sha


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check():
    run = Path("artifacts/runs/r05-cold-replication-20260916")
    series = read(run / "series.json")
    report = Path(series["configuration"]["report_directory"])
    analysis = read(report / "uncertainty.json")
    published = read(report / "results.json")
    original_path = Path(series["configuration"]["source_run"]) / "series.json"
    original = read(original_path)
    assert series["status"] == "completed" and analysis["status"] == "passed"
    assert analysis["source_series_sha256"] == file_sha(run / "series.json")
    assert file_sha(original_path) == series["configuration"]["source_series_sha256"]
    assert published["series"] == series and published["analysis"] == analysis
    assert published["renderer_sha256"] == file_sha(Path("src/evorec/research/replication_report.py"))
    assert analysis["analysis_source_sha256"] == file_sha(Path("src/evorec/research/replication_analysis.py"))
    assert analysis["bootstrap_source_sha256"] == file_sha(Path("src/evorec/research/uncertainty.py"))
    assert (Path("docs/experiments/archive") / (run.name + ".json")).read_bytes() == (run / "series.json").read_bytes()
    assert read(run / "configuration.json") == series["configuration"]
    assert series["source_configuration"] == original["configuration"]
    for name, digest in series["code"]["source_sha256"].items():
        frozen = run / "source" / name
        assert file_sha(frozen) == digest
        current = Path("src/evorec/research") / name
        assert current.read_bytes().replace(b"\r\n", b"\n") == frozen.read_bytes().replace(b"\r\n", b"\n")
        committed = subprocess.run(
            ["git", "show", series["code"]["git_base_commit"] + ":" + current.as_posix()],
            check=True, capture_output=True).stdout
        assert committed == frozen.read_bytes().replace(b"\r\n", b"\n"), name
    repeated = read("docs/validation/replication-analysis-repeat.json")
    assert repeated["status"] == "passed" and repeated["identical_full_analysis_on_repeat"]
    assert repeated["analysis_sha256"] == file_sha(report / "uncertainty.json")
    assert series["code"]["experiment_paths_clean"]
    checkpoints = []
    for trial in series["trials"]:
        path = checked_file(trial["checkpoint"])
        saved = torch.load(path, map_location="cpu", weights_only=True)
        assert saved["protocol_id"] == series["protocol_id"]
        assert saved["feature_fingerprint"] == series["feature_fingerprint"]
        assert saved["scalar_names"] == list(SCALAR_NAMES)
        assert saved["model_config"] == original["configuration"]["model"]
        assert saved["training_cache_sha256"] == original["training_cache"]["sha256"]
        assert saved["seed"] == trial["seed"] and saved["epoch"] == trial["best_epoch"]
        assert trial["checkpoint_reload_verified"]
        best = max(trial["history"], key=lambda row: row["validation_ndcg@10"])
        assert best["epoch"] == trial["best_epoch"]
        if trial["seed"] == 17:
            source_trial = next(t for t in original["trials"] if t["name"] == trial["name"])
            reference = torch.load(checked_file(source_trial["checkpoint"]), map_location="cpu", weights_only=True)
            assert checkpoint_states_equal(saved, reference)
            assert trial["original_replay_verified"]
        checkpoints.append({"seed": trial["seed"], "best_epoch": trial["best_epoch"],
                            "epochs": len(trial["history"]), "sha256": file_sha(path)})
    assert [t["seed"] for t in series["trials"]] == [17, 29, 43]
    assert len(analysis["intervals"]) == 48
    assert analysis["original_seed17_test_rankings_identical"]
    assert all(row["low"] <= row["high"] for row in analysis["intervals"])
    assert len({(r["method"], r["baseline"], r["cohort"], r["metric"])
                for r in analysis["intervals"]}) == 48
    for record in series["source_inputs"].values():
        checked_file(record)
    assets = Path("docs/blog/assets/evorec/r05-replication")
    # Publishing assets are optional, local-only material.
    blog_image_count = 0
    if assets.exists():
        manifest = read(assets / "manifest.json")
        assert manifest["generator_sha256"] == file_sha(Path(manifest["generator"]))
        assert len(manifest["assets"]) == 4
        for row in manifest["assets"]:
            path = assets / row["file"]
            assert file_sha(path) == row["sha256"] == file_sha(Path(row["source_image"]))
            assert file_sha(Path(row["source_results"])) == row["source_results_sha256"]
            if path.suffix == ".png":
                with Image.open(path) as picture:
                    picture.verify()
            else:
                assert ET.parse(path).getroot().tag.endswith("svg")
        blog_image_count = len(manifest["assets"])
    tests = {}
    for name, expected in (("replication-core-tests.xml", 96),
                           ("replication-implementation-tests.xml", 18),
                           ("replication-ranker-tests.xml", 9)):
        document = ET.parse(Path("docs/validation") / name).getroot()
        assert not document.findall(".//failure") and not document.findall(".//error")
        cases = document.findall(".//testcase")
        passed = sum(c.find("skipped") is None for c in cases)
        assert passed == expected
        tests[name] = {"passed": passed, "skipped": len(cases) - passed}
    local_links = 0
    documents = [Path(p) for p in subprocess.check_output(["git", "ls-files", "-z"]).decode("utf-8").split("\0") if p.endswith(".md")]
    for path in documents:
        for target in re.findall(r"!?\[[^\]\n]*\]\(([^)\n]+)\)", path.read_text(encoding="utf-8-sig")):
            target = target.strip().strip("<>")
            if target.startswith(("http:", "https:", "#", "mailto:")):
                continue
            target = unquote(target.split("#", 1)[0])
            if target:
                assert (path.parent / target).exists(), (path, target)
                local_links += 1
    for path in list(Path("src").rglob("*.py")) + list(Path("scripts").glob("*.py")) + list(Path("tests").glob("*.py")):
        ast.parse(path.read_text(encoding="utf-8-sig"))
    subprocess.run(["git", "diff", "--check"], check=True, capture_output=True)
    result = {
        "status": "passed", "checker_sha256": file_sha(Path(__file__)),
        "series_sha256": file_sha(run / "series.json"),
        "analysis_sha256": file_sha(report / "uncertainty.json"),
        "code_commit": series["code"]["git_base_commit"],
        "experiment_paths_clean_at_start": series["code"]["experiment_paths_clean"],
        "unrelated_worktree_changes_at_start": series["code"]["unrelated_worktree_changes"],
        "source_and_archive_unchanged": True,
        "source_files_match_training_commit_after_eol_normalization": True,
        "identical_full_analysis_on_repeat": True,
        "checkpoints": checkpoints,
        "audited_queries": analysis["audited_queries"],
        "audited_candidate_checks": analysis["audited_candidate_checks"],
        "confidence_intervals": 48, "blog_image_files_checked": blog_image_count,
        "markdown_local_links_checked": local_links, "tests": tests,
        "limits": ["historical R05 already inspected; not a fresh test",
                   "current model checkpoints are local ignored artifacts",
                   "provider attribution cannot be reconstructed from saved traces"],
    }
    Path("docs/validation/replication-artifacts-checks.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    check()
