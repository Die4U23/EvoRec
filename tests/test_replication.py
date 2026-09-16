import gzip
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
torch = pytest.importorskip("torch")
pytest.importorskip("sklearn")
import numpy as np

from evorec.research.baselines import Ranking
from evorec.research.data import Event
from evorec.research.protocol import Query, summarize
from evorec.research.replicate_ranker import checked_file, checkpoint_states_equal
from evorec.research.replication_analysis import query_users, trace_values
from evorec.research.runner import file_sha
from evorec.research.train import write_trace


def test_fingerprint_rejects_modified_input(tmp_path):
    path = tmp_path / "input"
    path.write_bytes(b"original")
    record = {"path_from_project_root": str(path), "sha256": file_sha(path)}
    assert checked_file(record) == path
    path.write_bytes(b"changed")
    with pytest.raises(ValueError):
        checked_file(record)


def test_state_comparison_detects_weight_or_schema_change():
    a = {"state_dict": {"weight": torch.tensor([1., 2.])}}
    assert checkpoint_states_equal(a, a)
    assert not checkpoint_states_equal(a, {"state_dict": {"weight": torch.tensor([1., 3.])}})
    assert not checkpoint_states_equal(a, {"state_dict": {"other": torch.tensor([1., 2.])}})


def fixture(tmp_path):
    query = Query("q", 100, "target", ("history",), frozenset({"history"}), True, True)
    protocol = SimpleNamespace(catalog={"history": 1, "target": 2, "other": 3},
                               catalog_times=[1, 2, 3])
    rankings = [Ranking(("target", "other"))]
    trace = write_trace(tmp_path / "trace.gz", [query], rankings)
    expected = summarize(protocol, [query], rankings)
    return query, protocol, trace, expected


def test_trace_audit_recomputes_rank_and_rejects_candidate_injection(tmp_path):
    query, protocol, trace, expected = fixture(tmp_path)
    values, audit = trace_values(trace, [query], protocol, expected, np.array([[1, 2]]),
                                ["target", "other"])
    assert values.tolist() == [[1., 1.]] and audit["candidate_checks"] == 2
    with pytest.raises(ValueError, match="pool"):
        trace_values(trace, [query], protocol, expected, np.array([[1, 0]]),
                     ["target", "other"])
    with pytest.raises(ValueError, match="identity"):
        trace_values(trace, [replace(query, target="other")], protocol, expected,
                     np.array([[1, 2]]), ["target", "other"])


def test_trace_audit_does_not_trust_saved_metrics(tmp_path):
    query, protocol, trace, expected = fixture(tmp_path)
    path = tmp_path / "trace.gz"
    with gzip.open(path, "rt") as stream:
        row = json.loads(stream.readline())
    row["metrics"]["ndcg@10"] = 0
    with gzip.open(path, "wt") as stream:
        stream.write(json.dumps(row) + "\n")
    trace["sha256"] = file_sha(path)
    with pytest.raises(ValueError, match="actual rank"):
        trace_values(trace, [query], protocol, expected, np.array([[1, 2]]), ["target", "other"])


def test_user_mapping_requires_exact_query_id_set():
    event = Event("u", "item", 5., 100)
    query = Query("incorrect", 100, "item", (), frozenset(), True, False)
    with pytest.raises(ValueError, match="one-to-one"):
        query_users([event], [query], {"validation_end_ms": 50, "positive_rating_min": 4})


def test_attribution_not_present_in_trace_is_not_fabricated(tmp_path):
    query, protocol, trace, _ = fixture(tmp_path)
    attributed = [Ranking(("target", "other"), 1, 1)]
    expected = summarize(protocol, [query], attributed)
    _, audit = trace_values(trace, [query], protocol, expected,
                            np.array([[1, 2]]), ["target", "other"])
    assert "mean_personalized_candidates" in audit["unreconstructed_fields"]
