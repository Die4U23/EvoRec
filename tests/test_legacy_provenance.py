import importlib.util
from pathlib import Path


def load_checker():
    path = Path("scripts/check_legacy_provenance.py")
    spec = importlib.util.spec_from_file_location("check_legacy_provenance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_r02_r05_exact_sources_remain_reconstructable():
    result = load_checker().check()

    assert result["status"] == "passed"
    assert result["runs_checked"] == 4
    assert all(run["exact_source_reconstructable"] for run in result["runs"])
    assert all(run["original_working_tree_dirty"] for run in result["runs"])
