import csv
import json

import pytest

from evorec.research.baselines import Ranking
from evorec.research.protocol import AvailableAt, Protocol, summarize
from evorec.research.runner import file_sha
from evorec.research.sample import selected_user


@pytest.fixture
def protocol(tmp_path):
    t = 1650000000000
    sample = tmp_path / "sample.csv"
    sample.write_text(
        "user_id,parent_asin,rating,timestamp\n"
        f"u,a,5,{t}\n"
        f"v,b,5,{t}\n"
        f"u,b,5,{t+1}\n"
        f"u,z,1,{t+2}\n"
        f"u,c,5,{t+10}\n"
        f"v,d,5,{t+10}\n"
        f"w,c,5,{t+20}\n", encoding="utf-8",
    )
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"a": t-1, "b": t-1, "c": t-1, "d": t+10, "z": t-1}))
    sample.with_suffix(".manifest.json").write_text(json.dumps({
        "status": "completed_user_hash_sample", "sample_sha256": file_sha(sample),
        "catalog_path": str(catalog), "catalog_sha256": file_sha(catalog), "rows": 7,
    }))
    return Protocol({
        "dataset_path": str(sample), "train_end_ms": t+10, "validation_end_ms": t+20,
        "positive_rating_min": 4, "history_limit": 50,
    })


def test_test_split_is_sealed_until_explicit_final_evaluation(protocol):
    with pytest.raises(ValueError, match="sealed"):
        protocol.queries("test")
    assert len(protocol.queries("test", test_authorized=True)) == 1


def test_vocab_and_examples_use_training_positives_only(protocol):
    assert protocol.vocabulary == ("a", "b")
    assert protocol.training_examples() == [((1,), 2)]
    assert "z" in protocol.train_items and "z" not in protocol.vocabulary
    assert "c" not in protocol.train_items


def test_full_catalog_and_histories_have_separate_sources(protocol):
    queries = protocol.queries("validation")
    assert len(queries) == 2
    first, second = queries
    assert first.history == ("a", "b")
    assert first.seen == frozenset({"a", "b", "z"})
    assert first.target_available is True and first.target_model_cold is True
    assert second.target_available is False
    assert "d" not in AvailableAt(protocol.catalog, second.timestamp_ms)


def test_metrics_keep_unavailable_targets_and_empty_cohorts(protocol):
    queries = protocol.queries("validation")
    result = summarize(protocol, queries, [Ranking(("c",)), Ranking(("a",))])
    assert result["cohorts"]["all_positive_events"]["n"] == 2
    assert result["cohorts"]["all_positive_events"]["ndcg@10"] == .5
    assert result["cohorts"]["available_target"]["n"] == 1
    assert result["cohorts"]["cold_user"]["ndcg@10"] is None
    assert result["cohorts"]["all_positive_events"]["ranking_latency_ms_p95"] is None


def test_protocol_rejects_bad_candidate_and_missing_result(protocol):
    queries = protocol.queries("validation")
    with pytest.raises(ValueError, match="invalid candidates"):
        summarize(protocol, queries, [Ranking(("a",)), Ranking(("a",))])
    with pytest.raises(ValueError, match="count differs"):
        summarize(protocol, queries, [Ranking(("c",))])


def test_hash_sampling_keeps_user_membership_stable_and_excludes_development_users():
    selected = [f"user-{i}" for i in range(1000) if selected_user(f"user-{i}", set())]
    assert selected
    for user in selected:
        assert selected_user(user, set())
        assert not selected_user(user, {user})
    with pytest.raises(ValueError):
        selected_user("u", set(), 0)
