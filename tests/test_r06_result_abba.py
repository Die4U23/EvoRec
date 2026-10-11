"""Two-source controls must not hide failures, swap arms or change the harness."""

from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4
from subprocess import CalledProcessError

import pytest

from evorec.infrastructure.model_runtime import ControlledLoadError
from scripts import check_r06_result_abba as checker
from test_r06_trace_abba import documents, write


CANDIDATE = "e" * 40
OLD = {name: "a" * 64 for name in checker.CANDIDATE_FILES}
OLD.update({"src/example.py": "b" * 64, checker.PROFILE: "c" * 64,
            "scripts/r06_service_lab.py": "d" * 64})
NEW = {**OLD, **checker.CANDIDATE_FILES, checker.PROFILE: "f" * 64, checker.SELF: "9" * 64}


def rounds(control, candidate, statuses=None):
    for name, variant in checker.ORDER:
        docs = documents(False, statuses)
        commit, hashes, root = ((checker.CONTROL, OLD, control) if variant == "A"
                                else (CANDIDATE, NEW, candidate))
        docs["profile"].update(source_commit=commit, source_sha256=hashes)
        docs["observations"]["source_commit"] = commit
        write(root / name, docs)


@pytest.mark.parametrize("statuses", [[200] * 26, [504] * 26, [200, 504] * 13])
def test_fixed_untraced_series_retains_every_failure(tmp_path, statuses):
    control, candidate = tmp_path / "old", tmp_path / "new"
    rounds(control, candidate, statuses)
    result = checker.check_series(control, candidate, CANDIDATE, OLD, NEW)
    assert result["order"] == ["a1", "b1", "b2", "a2"]
    assert [row["variant"] for row in result["arms"]] == ["A", "B", "B", "A"]
    assert sum(row["load"]["requests"] for row in result["arms"]) == 96
    assert sum(row["load"]["failures"] for row in result["arms"]) == 4 * statuses[2:].count(504)
    assert result["zero_failures"] is (504 not in statuses)
    assert result["live_resource_checks_performed"] is False
    assert result["production_acceptance"] is False and result["sla_proven"] is False
    assert all(row["asgi_timings"] is None for row in result["arms"])


@pytest.mark.parametrize("defect", ["wrong_source", "wrong_hash", "hidden_failure", "traced", "missing_arm", "reused"])
def test_rejects_swapped_sources_forged_green_and_incomplete_series(tmp_path, defect):
    control, candidate = tmp_path / "old", tmp_path / "new"
    rounds(control, candidate, [200] * 25 + [504])
    path = candidate / "b1/profile.json"
    data = json.loads(path.read_bytes())
    if defect == "wrong_source": data["source_commit"] = checker.CONTROL
    elif defect == "wrong_hash": data["source_sha256"] = OLD
    elif defect == "hidden_failure": data["load"]["failures"] = 0
    elif defect == "traced": data["trace_enabled"] = True
    elif defect == "missing_arm": path.unlink()
    elif defect == "reused":
        for suffix in ("owned", "stopped", "process-1/ready", "process-1/stopped"):
            previous = json.loads((candidate / "b1" / (suffix + ".json")).read_bytes())
            current_path = candidate / "b2" / (suffix + ".json")
            current = json.loads(current_path.read_bytes())
            current["run_id"] = previous["run_id"]
            if "schema" in current: current["schema"] = previous["schema"]
            current_path.write_text(json.dumps(current), encoding="utf-8")
    if path.exists(): path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        checker.check_series(control, candidate, CANDIDATE, OLD, NEW)


@pytest.mark.parametrize("defect", [None, "extra_production", "missing_production", "wrong_candidate", "worker",
                                   "lab", "other_script", "client", "import", "missing_helper"])
def test_source_pair_allows_only_registered_delta_and_inactive_helper(tmp_path, defect):
    control, candidate = tmp_path / "old", tmp_path / "new"
    source = "import os\ndef sample(): return 1\ndef _fresh_database_trace_complete(row): return False\n"
    for root in (control, candidate):
        (root / "scripts").mkdir(parents=True)
        (root / checker.PROFILE).write_text(source, encoding="utf-8")
    (candidate / checker.PROFILE).write_text(source.replace("return False", "return True"), encoding="utf-8")
    old, new = deepcopy(OLD), deepcopy(NEW)
    if defect == "extra_production": new["src/extra.py"] = "1" * 64
    elif defect == "missing_production": del new["src/example.py"]
    elif defect == "wrong_candidate": new[next(iter(checker.CANDIDATE_FILES))] = "1" * 64
    elif defect == "worker": new["src/example.py"] = "1" * 64
    elif defect == "lab": new["scripts/r06_service_lab.py"] = "1" * 64
    elif defect == "other_script": new["scripts/extra.py"] = "1" * 64
    elif defect == "client": (candidate / checker.PROFILE).write_text(source.replace("return 1", "return 2"))
    elif defect == "import": (candidate / checker.PROFILE).write_text(source.replace("import os", "import sys"))
    elif defect == "missing_helper": (candidate / checker.PROFILE).write_text("def sample(): return 1\n")
    if defect is None:
        checker.check_source_pair(control, candidate, old, new)
    else:
        with pytest.raises(ValueError): checker.check_source_pair(control, candidate, old, new)


def test_cli_rejection_does_not_leak_private_input(tmp_path, capsys):
    assert checker.main([str(tmp_path), "--candidate-commit", CANDIDATE, "--arm", "b1", "--save"]) == 1
    rejected = json.loads(capsys.readouterr().out)
    assert rejected == {"status": "old_new_diagnostic_rejected", "failed_check": "save requires complete series"}


def cli_roots(tmp_path, monkeypatch):
    control = tmp_path / "old"
    control.mkdir()
    candidate = Path(checker.__file__).resolve().parents[1]
    relative = Path("artifacts/test-result-abba") / uuid4().hex
    monkeypatch.setattr(checker, "RELATIVE", relative)
    monkeypatch.setattr(checker, "_source", lambda root: checker.CONTROL if root == control else CANDIDATE)
    monkeypatch.setattr(checker, "subprocess_sources", lambda root: OLD if root == control else NEW)
    monkeypatch.setattr(checker, "check_source_pair", lambda *args: None)
    roots = control / relative, candidate / relative
    rounds(*roots)
    return control, candidate, roots


def test_cli_partial_not_series_and_save_never_overwrites(tmp_path, monkeypatch, capsys):
    control, _, roots = cli_roots(tmp_path, monkeypatch)
    args = [str(control), "--candidate-commit", CANDIDATE]
    assert checker.main([*args, "--arm", "a1"]) == 0
    partial = json.loads(capsys.readouterr().out)
    assert partial["source_commit"] == checker.CONTROL and "arms" not in partial
    assert checker.main([*args, "--save"]) == 0
    saved = (roots[1] / "series-check.json").read_bytes()
    assert json.loads(saved)["candidate_commit"] == CANDIDATE
    assert checker.main([*args, "--save"]) == 1
    assert (roots[1] / "series-check.json").read_bytes() == saved


@pytest.mark.parametrize("defect", ["same_root", "wrong_control", "wrong_candidate", "changed_source", "changed_hash"])
def test_cli_enforces_both_clean_sources_and_rechecks_after_parsing(tmp_path, monkeypatch, capsys, defect):
    control, candidate, roots = cli_roots(tmp_path, monkeypatch)
    if defect == "same_root": control = candidate
    elif defect == "wrong_control": monkeypatch.setattr(checker, "_source", lambda _: CANDIDATE)
    elif defect == "wrong_candidate": monkeypatch.setattr(checker, "_source", lambda _: checker.CONTROL)
    elif defect == "changed_source":
        source = iter([checker.CONTROL, CANDIDATE, checker.CONTROL, "9" * 40])
        monkeypatch.setattr(checker, "_source", lambda _: next(source))
    elif defect == "changed_hash":
        hashes = iter([OLD, NEW, {**OLD, "src/example.py": "1" * 64}])
        monkeypatch.setattr(checker, "subprocess_sources", lambda _: next(hashes))
    assert checker.main([str(control), "--candidate-commit", CANDIDATE, "--save"]) == 1
    assert not (roots[1] / "series-check.json").exists()
    assert json.loads(capsys.readouterr().out)["status"] == "old_new_diagnostic_rejected"


@pytest.mark.parametrize("error", [ControlledLoadError("source_dirty", "PRIVATE-CONTENT"),
                                 CalledProcessError(1, "PRIVATE-COMMAND")])
def test_cli_dirty_or_unavailable_git_rejects_without_private_error_text(tmp_path, monkeypatch, capsys, error):
    control, _, _ = cli_roots(tmp_path, monkeypatch)
    def unavailable(_):
        raise error
    monkeypatch.setattr(checker, "_source", unavailable)
    assert checker.main([str(control), "--candidate-commit", CANDIDATE]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "old_new_diagnostic_rejected"
    assert "PRIVATE" not in output
