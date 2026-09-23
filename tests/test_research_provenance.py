import subprocess

import pytest

from evorec.research import runner


def test_clean_experiment_snapshot_records_commit_tree_and_unrelated_changes(monkeypatch):
    monkeypatch.setattr(
        runner,
        "git_snapshot",
        lambda: {"git_base_commit": "a" * 40, "working_tree_dirty": True},
    )

    def check_output(command, **_kwargs):
        if command[-1] == "HEAD^{tree}":
            return "b" * 40 + "\n"
        if "--" in command:
            return ""
        return " M README.md\n"

    monkeypatch.setattr(subprocess, "check_output", check_output)

    result = runner.clean_experiment_snapshot(("src/evorec/research",))

    assert result["git_base_commit"] == "a" * 40
    assert result["git_tree"] == "b" * 40
    assert result["working_tree_dirty"] is True
    assert result["experiment_paths_clean"] is True
    assert result["experiment_scopes"] == ["src/evorec/research"]
    assert result["unrelated_worktree_changes"] == [" M README.md"]


def test_clean_experiment_snapshot_rejects_scoped_changes(monkeypatch):
    monkeypatch.setattr(
        runner,
        "git_snapshot",
        lambda: {"git_base_commit": "a" * 40, "working_tree_dirty": True},
    )

    def check_output(command, **_kwargs):
        if command[-1] == "HEAD^{tree}":
            return "b" * 40 + "\n"
        return " M src/evorec/research/train.py\n"

    monkeypatch.setattr(subprocess, "check_output", check_output)

    with pytest.raises(RuntimeError, match="commit experiment code"):
        runner.clean_experiment_snapshot(("src/evorec/research",))


def test_clean_experiment_snapshot_requires_git(monkeypatch):
    monkeypatch.setattr(
        runner,
        "git_snapshot",
        lambda: {"git_base_commit": None, "working_tree_dirty": None},
    )

    with pytest.raises(RuntimeError, match="require a Git checkout"):
        runner.clean_experiment_snapshot()
