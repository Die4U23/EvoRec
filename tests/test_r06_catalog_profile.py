"""Observation helpers preserve facts and reject noncanonical cost references."""

import hashlib
from types import SimpleNamespace

import pytest

from scripts.profile_r06_catalog_capture import _baseline_sql, _capture, _reference, _sql_literal, plan_nodes


def test_plan_facts_preserve_zero_execution_and_spill_without_estimate_claims():
    raw = [{"Plan": {"Node Type": "Aggregate", "Plan Rows": 999,
                     "Actual Rows": 1, "Actual Loops": 1, "Plans": [
        {"Node Type": "Seq Scan", "Relation Name": "items", "Actual Rows": 0,
         "Actual Loops": 0, "Temp Read Blocks": 3, "Temp Written Blocks": 7},
        {"Node Type": "Sort", "Sort Method": "external merge", "Sort Space Used": 42,
         "Sort Space Type": "Disk"}]}}]
    facts = plan_nodes(raw)
    assert len(facts) == 3 and "Plan Rows" not in facts[0]
    assert facts[1]["Actual Loops"] == 0 and facts[1]["Temp Written Blocks"] == 7
    assert facts[2]["Sort Space Type"] == "Disk"


def test_published_cost_reference_has_ordered_fingerprints():
    rows = [dict(item_id="目录é", internal_item_id=0, is_active=True,
                 r06_model_text="actual text", r06_first_seen_ms=-1)]
    runtime = _reference(rows)
    assert runtime.item_ids == ("目录é",)
    assert runtime.full_eligible_items == frozenset({"目录é"})
    assert len(runtime.member_sha256) == len(runtime.full_active_sha256) == 32
    assert runtime.catalog_items["目录é"].first_seen_ms == -1


@pytest.mark.parametrize("field,value", [("internal_item_id", 1), ("is_active", False),
                                        ("r06_model_text", None), ("r06_first_seen_ms", None)])
def test_cost_reference_rejects_unexpected_initial_catalog(field, value):
    row = dict(item_id="a", internal_item_id=0, is_active=True,
               r06_model_text="text", r06_first_seen_ms=0)
    row[field] = value
    with pytest.raises(ValueError, match="not canonical"):
        _reference([row])


def test_baseline_reads_literal_without_executing_historical_code():
    source = 'raise RuntimeError("must not run")\nCATALOG_CAPTURE_SQL = "SELECT literal"\n'
    assert _sql_literal(source) == "SELECT literal"


@pytest.mark.parametrize("source", ["", 'CATALOG_CAPTURE_SQL = str("SELECT")',
                                   "CATALOG_CAPTURE_SQL = 1",
                                   'CATALOG_CAPTURE_SQL = "a"\nCATALOG_CAPTURE_SQL = "b"'])
def test_baseline_rejects_missing_dynamic_or_duplicate_sql(source):
    with pytest.raises(ValueError, match="exactly one literal"):
        _sql_literal(source)


@pytest.mark.parametrize("commit", ["", "HEAD", "main", "-x", "f"*39, "F"*40, "f"*41])
def test_baseline_requires_immutable_full_sha_before_git(tmp_path, monkeypatch, commit):
    def forbidden(*args, **kwargs):
        pytest.fail("git must not run for invalid baseline identifier")
    monkeypatch.setattr("scripts.profile_r06_catalog_capture.subprocess.run", forbidden)
    with pytest.raises(ValueError, match="full lowercase commit SHA"):
        _baseline_sql(tmp_path, commit)


def test_baseline_preserves_git_bytes_and_literal_hashes(tmp_path, monkeypatch):
    commit = "f"*40
    raw = b'CATALOG_CAPTURE_SQL = "SELECT literal"\r\n'
    calls = []
    def read_git(args, **kwargs):
        calls.append(args)
        assert kwargs == dict(cwd=tmp_path, check=True, capture_output=True)
        return SimpleNamespace(stdout=b"commit\n" if args[1] == "cat-file" else raw)
    monkeypatch.setattr("scripts.profile_r06_catalog_capture.subprocess.run", read_git)
    query, source = _baseline_sql(tmp_path, commit)
    assert calls == [["git", "cat-file", "-t", commit],
                     ["git", "show", f"{commit}:src/evorec/infrastructure/r06_catalog_capture.py"]]
    assert source == dict(commit=commit, sql_sha256=hashlib.sha256(query.encode()).hexdigest(),
                          module_sha256=hashlib.sha256(raw).hexdigest())


def test_baseline_rejects_noncommit_git_object(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.profile_r06_catalog_capture.subprocess.run",
                        lambda *args, **kwargs: SimpleNamespace(stdout=b"tree\n"))
    with pytest.raises(ValueError, match="must name a commit"):
        _baseline_sql(tmp_path, "f"*40)


def test_summary_conversion_does_not_mutate_driver_row():
    row = dict(member_count=1, member_sha256=b"m", active_count=0, active_sha256=b"a",
               invalid_active_count=0, inactive_ids=["item"])
    assert _capture(row).inactive_ids == ("item",)
    assert row["inactive_ids"] == ["item"]
