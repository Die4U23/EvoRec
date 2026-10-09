"""Microbenchmark controls only, never wall-time or memory-budget acceptance."""

import json

import pytest

from scripts import profile_r06_membership as probe


@pytest.mark.parametrize("changes", [{"items": True}, {"items": 0}, {"items": 150001},
                                     {"blocks": True}, {"blocks": 0}, {"blocks": 17}])
def test_limits_reject_before_source_or_allocations(monkeypatch, changes):
    monkeypatch.setattr(probe, "_source", lambda *_: pytest.fail("source reached"))
    with pytest.raises(ValueError):
        probe.profile(**changes)


def test_existing_allocation_observer_rejected_before_source(monkeypatch):
    monkeypatch.setattr(probe.tracemalloc, "is_tracing", lambda: True)
    monkeypatch.setattr(probe, "_source", lambda *_: pytest.fail("source reached"))
    with pytest.raises(ValueError, match="observer"):
        probe.profile(items=3, blocks=1)


def test_small_report_preserves_order_scope_and_separate_allocation_pass(monkeypatch):
    monkeypatch.setattr(probe, "_source", lambda *_: "synthetic-source")
    monkeypatch.setattr(probe, "subprocess_sources", lambda *_: {"synthetic": "hash"})
    report = probe.profile(items=3, blocks=2)
    assert [record["implementation"] for record in report["timings"]] == [
        "reference", "candidate", "candidate", "reference"] * 2
    assert set(report["median_seconds"]) == set(report["traced_peak_bytes"]) == {"reference", "candidate"}
    assert len(report["timings"]) == 8
    assert all(record["elapsed_seconds"] >= 0 for record in report["timings"])
    assert report["kind"] == "synthetic_membership_predicate_not_database_or_api"
    assert report["source_commit"] == "synthetic-source"
    assert report["source_sha256"] == {"synthetic": "hash"}
    assert report["automatic_retries"] == 0 and report["gc_policy_changed"] is False
    assert not probe.tracemalloc.is_tracing()
    json.dumps(report)


@pytest.mark.parametrize("change", ["commit", "hashes"])
def test_source_change_rejects_report(monkeypatch, change):
    commits = iter(["before", "after" if change == "commit" else "before"])
    hashes = iter([{"source": "before"}, {"source": "after"}])
    monkeypatch.setattr(probe, "_source", lambda *_: next(commits))
    monkeypatch.setattr(probe, "subprocess_sources", lambda *_: next(hashes))
    with pytest.raises(ValueError, match="source changed"):
        probe.profile(items=3, blocks=1)


@pytest.mark.parametrize("name", ["outside.json", "existing"])
def test_output_boundary_rejected_before_measurement(monkeypatch, tmp_path, name):
    # basetemp may itself live inside real artifacts; use an owned fake project.
    monkeypatch.setattr(probe, "__file__", str(tmp_path / "scripts" / "profile.py"))
    output = tmp_path / name
    if name == "existing":
        output = tmp_path / "artifacts" / "existing.json"
        output.parent.mkdir()
        output.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(probe, "profile", lambda **_: pytest.fail("measurement reached"))
    with pytest.raises(ValueError, match="output"):
        probe.main([str(output)])
    if name == "existing":
        assert output.read_text(encoding="utf-8") == "preserve"


def test_new_owned_output_is_written_once(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "__file__", str(tmp_path / "scripts" / "profile.py"))
    report = dict(kind="synthetic", items=3, median_seconds={}, traced_peak_bytes={})
    monkeypatch.setattr(probe, "profile", lambda **_: report)
    output = tmp_path / "artifacts" / "new.json"
    assert probe.main([str(output), "--items", "3", "--blocks", "1"]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == report
    with pytest.raises(ValueError, match="output"):
        probe.main([str(output)])
