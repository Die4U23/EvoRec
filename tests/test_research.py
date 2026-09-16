import gzip
import io
import json

import pytest

from evorec.research.baselines import ItemCF, Popular, Ranking
from evorec.research.data import Event, load_events, parse_row, split_name
from evorec.research.download import FIELDS, LimitedReader, collect_prefix
from evorec.research.evaluation import Aggregate, evaluate, ranking_metrics
from evorec.research.runner import file_sha, run


def event(user, item, timestamp, rating=5):
    return Event(user, item, rating, timestamp)


@pytest.mark.parametrize("changes", [
    {"timestamp": "1650000000"}, {"timestamp": "-1"}, {"rating": "NaN"},
    {"rating": "inf"}, {"rating": "6"}, {"user_id": " "},
])
def test_source_parser_rejects_wrong_units_and_invalid_values(changes):
    row = dict(zip(FIELDS, ("user", "item", "4", "1650000000000")))
    with pytest.raises(ValueError):
        parse_row({**row, **changes})


def test_source_schema_rejects_unexpected_fields():
    row = dict(zip(FIELDS, ("user", "item", "4", "1650000000000")))
    with pytest.raises(ValueError):
        parse_row({**row, "history": "future-item"})


def test_dedup_keeps_earliest_rating_instead_of_future_positive(tmp_path):
    path = tmp_path / "events.csv"
    path.write_text(
        "user_id,parent_asin,rating,timestamp\n"
        "u,a,5,1650000000002\n"
        "u,a,1,1650000000000\n"
        "v,b,4,1650000000001\n", encoding="utf-8",
    )
    events, stats = load_events(path)
    assert len(events) == 2
    assert events[0].item_id == "a" and events[0].rating == 1
    assert stats["duplicates_removed"] == 1


@pytest.mark.parametrize("timestamp,expected", [(9, "train"), (10, "validation"), (19, "validation"), (20, "test")])
def test_global_split_exact_boundaries(timestamp, expected):
    assert split_name(timestamp, 10, 20) == expected


def test_popular_uses_unique_positive_pairs_and_stable_ties():
    train = [event("u", "a", 1), event("u", "a", 1), event("v", "b", 1), event("v", "negative", 1, 1)]
    model = Popular().fit(train)
    assert model.counts == {"a": 1, "b": 1}
    assert model.rank((), set(), {"a", "b", "future"}).items == ("a", "b")
    assert model.rank((), {"a"}, {"a", "b"}).items == ("b",)
    assert model.rank((), set(), set()).items == ()


def test_itemcf_matches_hand_calculated_binary_cosine():
    train = [
        event("u1", "a", 1), event("u1", "b", 2),
        event("u2", "a", 1), event("u2", "c", 2),
        event("u3", "a", 1), event("u3", "b", 2),
    ]
    model = ItemCF().fit(train)
    scores = dict(model.neighbors["a"])
    assert scores["b"] == pytest.approx(2 / (3 * 2) ** .5)
    assert scores["c"] == pytest.approx(1 / 3 ** .5)
    result = model.rank(("a",), {"a"}, {"a", "b", "c"}, 2)
    assert result.items == ("b", "c")
    assert result.personalized_count == 2
    assert result.popularity_fill == 0


def test_itemcf_caps_use_latest_training_items_and_separate_popular_fill():
    model = ItemCF(max_user_items=2, neighbors=1).fit([
        event("u", "old", 1), event("u", "a", 2), event("u", "b", 3),
    ])
    assert "old" not in model.neighbors
    assert model.fit_stats["users_truncated"] == 1
    result = model.rank(("a",), {"a"}, {"old", "a", "b"})
    assert result.items == ("b", "old")
    assert result.personalized_count == 1 and result.popularity_fill == 1


def test_single_target_metrics_match_hand_calculation_and_boundaries():
    assert ranking_metrics(("a", "b", "target"), "target") == {
        "ndcg@10": .5, "recall@20": 1., "candidate_recall@200": 1.,
    }
    items = tuple(f"i{i}" for i in range(201))
    assert ranking_metrics(items, "i19")["recall@20"] == 1
    assert ranking_metrics(items, "i20")["recall@20"] == 0
    assert ranking_metrics(items, "i199")["candidate_recall@200"] == 1
    assert ranking_metrics(items, "i200")["candidate_recall@200"] == 0
    assert ranking_metrics(items, "missing")["ndcg@10"] == 0
    with pytest.raises(ValueError):
        ranking_metrics(("a", "a"), "a")


class SpyModel:
    name = "spy"

    def __init__(self):
        self.contexts = []

    def rank(self, history, seen, available, k):
        self.contexts.append((history, frozenset(seen), frozenset(available)))
        return Ranking(tuple(sorted(available - seen))[:k])


def test_same_timestamp_events_do_not_enter_history_or_catalog_early():
    events = [
        event("u", "a", 1), event("v", "b", 1, 1),
        event("u", "b", 10), event("u", "c", 10), event("w", "future", 10),
        event("u", "d", 20),
    ]
    model = SpyModel()
    trace = io.StringIO()
    result = evaluate(events, [model], 10, 20, request_output=trace)
    assert model.contexts[0] == (("a",), frozenset({"a"}), frozenset({"a", "b"}))
    assert model.contexts[1] == model.contexts[0]
    assert model.contexts[2][2] == frozenset({"a", "b"})
    assert model.contexts[3][0] == ("a", "b", "c")
    assert model.contexts[3][1] == frozenset({"a", "b", "c"})
    assert "future" in model.contexts[3][2] and "d" not in model.contexts[3][2]
    all_metrics = result["metrics"]["validation"]["spy"]["all_positive_events"]
    available_metrics = result["metrics"]["validation"]["spy"]["available_target"]
    assert all_metrics["n"] == 3 and all_metrics["recall@20"] == pytest.approx(1 / 3)
    assert available_metrics["n"] == 1 and available_metrics["recall@20"] == 1
    assert result["diagnostics"]["validation"]["target_unavailable_at_request"] == 2


def test_future_events_never_refit_popularity_and_input_order_is_irrelevant():
    events = [event("a", "known", 1), event("b", "new", 10), event("c", "new", 20)]
    model = Popular().fit(events[:1])
    before = model.counts.copy()
    first = evaluate(events, [model], 10, 20)
    second = evaluate(list(reversed(events)), [model], 10, 20)
    assert model.counts == before == {"known": 1}
    for split in ("validation", "test"):
        a = first["metrics"][split]["popular"]["all_positive_events"]
        b = second["metrics"][split]["popular"]["all_positive_events"]
        assert {k: v for k, v in a.items() if "latency" not in k} == {k: v for k, v in b.items() if "latency" not in k}
    assert first["metrics"]["test"]["popular"]["model_cold_available"]["n"] == 1
    assert first["metrics"]["test"]["popular"]["model_cold_available"]["candidate_recall@200"] == 0


def test_invalid_model_output_cannot_be_reported_as_valid_metrics():
    class Broken:
        name = "broken"

        def rank(self, history, seen, available, k):
            return Ranking(("future",))

    with pytest.raises(ValueError, match="unavailable"):
        evaluate([event("a", "known", 1), event("b", "future", 10)], [Broken()], 10, 20)


def test_empty_cohort_is_null_instead_of_zero_quality():
    result = Aggregate().report()
    assert result["n"] == 0
    assert result["ndcg@10"] is None
    assert result["ranking_latency_ms_p95"] is None


def test_prefix_download_stops_at_requested_rows():
    raw = (
        "user_id,parent_asin,rating,timestamp\n"
        "u,a,5,1650000000000\n"
        "v,b,4,1650000000001\n"
        "w,c,3,1650000000002\n"
    ).encode()
    source, output = io.BytesIO(raw), io.StringIO()
    assert collect_prefix(source, output, 2) == 2
    assert "w,c" not in output.getvalue()
    assert source.readline().startswith(b"w,c")
    with pytest.raises(ValueError, match="source ended"):
        collect_prefix(io.BytesIO(raw), io.StringIO(), 4)


def test_compressed_download_cannot_read_past_byte_budget():
    source = LimitedReader(io.BytesIO(b"123456789"), 5)
    assert source.read(3) == b"123"
    assert source.read(10) == b"45"
    with pytest.raises(ValueError, match="byte limit"):
        source.read(1)
    assert source.consumed == 5


def test_runner_rejects_checksum_mismatch_before_creating_run(tmp_path):
    dataset = tmp_path / "sample.csv"
    dataset.write_text("changed", encoding="utf-8")
    dataset.with_suffix(".manifest.json").write_text(json.dumps({
        "status": "completed_prefix_sample", "sample_sha256": "wrong",
    }))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "stage": "R01-feasibility", "positive_rating_min": 4,
        "train_end_ms": 1650000000001, "validation_end_ms": 1650000000002,
        "dataset_path": str(dataset),
    }))
    output = tmp_path / "run"
    with pytest.raises(ValueError, match="checksum"):
        run(config, output)
    assert not output.exists()


def test_recorded_run_matches_the_saved_request_trace(tmp_path):
    dataset = tmp_path / "sample.csv"
    dataset.write_text(
        "user_id,parent_asin,rating,timestamp\n"
        "u,a,5,1650000000000\n"
        "v,b,5,1650000000000\n"
        "u,b,5,1650000000001\n"
        "w,a,5,1650000000002\n", encoding="utf-8",
    )
    dataset.with_suffix(".manifest.json").write_text(json.dumps({
        "status": "completed_prefix_sample", "sample_sha256": file_sha(dataset), "rows": 4,
    }))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "stage": "R01-feasibility", "positive_rating_min": 4,
        "train_end_ms": 1650000000001,
        "validation_end_ms": 1650000000002,
        "dataset_path": str(dataset), "history_limit": 20,
        "itemcf_max_user_items": 100, "itemcf_neighbors": 100, "candidate_k": 200,
    }))
    result = run(config, tmp_path / "run")
    assert result["status"] == "completed"
    with gzip.open(tmp_path / "run/requests.jsonl.gz", "rt", encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]
    assert len(records) == 4
    assert all(record["metrics"]["ndcg@10"] == 1 for record in records)
    assert result["metrics"]["validation"]["popular"]["all_positive_events"]["ndcg@10"] == 1
    assert result["request_trace"]["sha256"] == file_sha(tmp_path / "run/requests.jsonl.gz")
