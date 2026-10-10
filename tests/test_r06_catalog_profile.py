"""Observation helpers preserve facts and reject noncanonical cost references."""

import pytest

from scripts.profile_r06_catalog_capture import _reference, plan_nodes


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
