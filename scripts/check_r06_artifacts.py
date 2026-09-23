"""Verify R06 delivery bindings and local-only preservation without changing the run."""
import argparse
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image
from check_gating_artifacts import Page
from evorec.research.ranker_data import read
from evorec.research.runner import file_sha


def check(run):
    branch = subprocess.check_output(["git", "branch", "--show-current"], text=True).strip()
    if branch != "codex/r06-multi-interest":
        raise ValueError("switch to the R06 branch before checking its delivery")
    s, analysis = read(run/"series.json"), read(run/"analysis.json")
    report = Path(s["configuration"]["report_directory"])
    published = read(report/"results.json")
    assert s["status"] == "completed" and analysis["status"] == "passed"
    assert published["series"] == s and published["analysis"] == analysis
    assert published["renderer_sha256"] == file_sha(Path("scripts/build_r06_report.py"))
    assert analysis["series_sha256"] == file_sha(run/"series.json")
    assert analysis["analysis_source_sha256"] == file_sha(Path("src/evorec/research/r06_analysis.py"))
    assert analysis["bootstrap_source_sha256"] == file_sha(Path("src/evorec/research/uncertainty.py"))
    assert file_sha(Path("research/configs/r06-multi-interest.json")) == s["configuration_sha256"]
    assert read(run/"configuration.json") == s["configuration"]
    assert (Path("docs/experiments/archive")/(run.name+".json")).read_bytes() == (run/"series.json").read_bytes()
    assert (report/"uncertainty.json").read_bytes() == (run/"analysis.json").read_bytes()
    for name, digest in s["code"]["source_sha256"].items():
        frozen = run/"source"/name
        current = Path("src/evorec/research")/name
        assert file_sha(frozen) == digest
        normalize = lambda value: value.replace(b"\r\n", b"\n")
        assert normalize(current.read_bytes()) == normalize(frozen.read_bytes())
        committed = subprocess.check_output(["git", "show", s["code"]["git_base_commit"]+":"+current.as_posix()])
        assert normalize(committed) == normalize(frozen.read_bytes())
    assert s["code"]["experiment_paths_clean"]
    assert all(value == 0 for value in s["user_overlaps"].values())
    assert s["A_training_matches_original"]
    assert len(s["trials"]) == 6 and sum(len(t["history"]) for t in s["trials"]) == 74
    assert all(t["status"] == "completed" and t["checkpoint_reload_verified"] for t in s["trials"])
    assert len(s["test_results"]) == len(s["validation_results"]) == 15
    assert len(analysis["intervals"]) == 50 and analysis["all_candidate_sources_and_models_replayed"]
    repeat = read("docs/validation/r06-interval-repeat.json")
    assert repeat["status"] == "passed" and repeat["intervals_recomputed"] == 50
    assert repeat["all_point_estimates_and_bounds_identical"]
    assert repeat["analysis_sha256"] == file_sha(run/"analysis.json")
    assert repeat["checker_sha256"] == file_sha(Path("scripts/verify_r06_intervals.py"))
    for name, key in (("source_run","source_series_sha256"), ("source_replication","source_replication_sha256")):
        assert file_sha(Path(s["configuration"][name])/"series.json") == s["configuration"][key]
    manifest = read(report/"figures/manifest.json")
    assert manifest["source_results_sha256"] == file_sha(report/"results.json")
    assert manifest["source_series_sha256"] == file_sha(run/"series.json")
    assert manifest["source_analysis_sha256"] == file_sha(run/"analysis.json")
    assert manifest["generator_sha256"] == file_sha(Path("scripts/build_r06_report.py"))
    assert len(manifest["assets"]) == 6
    for item in manifest["assets"]:
        path = report/"figures"/item["file"]
        assert file_sha(path) == item["sha256"]
        if path.suffix == ".png":
            with Image.open(path) as im:
                im.verify()
        else:
            assert ET.parse(path).getroot().tag.endswith("svg")
    page = Page()
    page.feed((report/"report.html").read_text(encoding="utf-8"))
    assert not page.stack and len(page.images) == 3
    assert all((report/target).exists() for target in page.images+page.links)
    tests = {}
    for name, expected, skips in (("r06-core-tests.xml",108,6), ("r06-implementation-tests.xml",47,0),
                                  ("repository-hygiene-tests.xml",12,0)):
        root = ET.parse(Path("docs/validation")/name).getroot()
        assert not root.findall(".//failure") and not root.findall(".//error")
        cases = root.findall(".//testcase")
        passed = sum(c.find("skipped") is None for c in cases)
        assert passed == expected and len(cases)-passed == skips
        tests[name] = {"passed": passed, "skipped": skips}
    local_proof = Path("tmp/r06-local-materials-preserved.json")
    preserved = 0
    if local_proof.exists():
        proof = read(local_proof)["preserved_files"]
        for name, digest in proof.items():
            assert Path(name).is_file() and file_sha(Path(name)) == digest
        preserved = len(proof)
    private_paths = [s["configuration"]["dataset_path"], str(run/"series.json"),
                     s["trials"][0]["checkpoint"]["path_from_project_root"], ".venv-research/pyvenv.cfg",
                     "docs/blog/evorec-project-log.md"]
    ignored = subprocess.check_output(["git", "check-ignore", *private_paths], text=True).splitlines()
    assert len(ignored) == len(private_paths)
    return {"status":"passed","branch":branch,"training_commit":s["code"]["git_base_commit"],
            "series_sha256":file_sha(run/"series.json"),"analysis_sha256":file_sha(run/"analysis.json"),
            "checker_sha256":file_sha(Path(__file__)),"training_source_matches_snapshot_and_commit":True,
            "original_R05_runs_unchanged":True,"local_materials_preserved":preserved,"figure_files_checked":6,
            "html_structure_and_resources":"passed; no browser rendering claim",
            "tests":tests,"test_overlap":"six protocol tests overlap across core and research; hygiene tests are also in core",
            "audited_source_queries":analysis["audited_source_queries"],
            "audited_ranking_queries":analysis["audited_ranking_queries"],
            "audited_candidate_checks":analysis["audited_candidate_checks"],"intervals_recomputed":50}


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",type=Path,required=True)
    parser.add_argument("--output",type=Path,default=Path("docs/validation/r06-artifacts-checks.json"))
    args=parser.parse_args()
    result=check(args.run)
    args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result))
