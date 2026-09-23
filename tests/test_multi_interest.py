"""Independent oracles for target isolation, budgets, temporal filters and test sealing."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
torch = pytest.importorskip("torch")
pytest.importorskip("sklearn")
import numpy as np

from evorec.research.baselines import Ranking
from evorec.research.content import ContentFeatures
from evorec.research.multi_interest import MultiInterestPredictor, recent_vectors, merge_content
from evorec.research.protocol import Query
from evorec.research.run_multi_interest import select_method, open_test
from evorec.research.r06_data import group_masks


class Fallback:
    def __init__(self, items):
        self.items = items

    def rank(self, history, seen, available, k):
        return Ranking(tuple(item for item in self.items if item in available and item not in seen)[:k])


def fixture():
    rng = np.random.default_rng(817)
    vectors = rng.normal(size=(24, 5)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1)[:, None]
    vectors[4] = 0
    features = ContentFeatures(tuple(f"item{i:02}" for i in range(24)), vectors)
    catalog = {item: index+1 for index, item in enumerate(features.items)}
    return features, catalog, Fallback(features.items)


def oracle(features, catalog, fallback, query, k):
    directions = []
    for distance, item in enumerate(reversed(query.history[-8:])):
        index = features.mapping.get(item)
        if index is not None and features.present[index]:
            directions.append((features.vectors[index], .9**distance))
    available = {item for item, first in catalog.items() if first < query.timestamp_ms}
    back = fallback.rank((), query.seen, available, k)
    if not directions:
        return back.items
    scored = []
    for item in features.items:
        index = features.mapping[item]
        if item in available and item not in query.seen and features.present[index]:
            score = max(weight*max(float(vector @ features.vectors[index]), 0.) for vector, weight in directions)
            scored.append((score, item))
    selected = [item for _, item in sorted(scored, key=lambda row: (-row[0], row[1]))[:k]]
    selected.extend(item for item in back.items if item not in selected)
    return tuple(selected[:k])


@pytest.mark.parametrize("batch_size", [1, 3, 8])
def test_gpu_style_batching_matches_independent_scalar_oracle(batch_size):
    f, catalog, fallback = fixture()
    queries = [SimpleNamespace(history=tuple(f.items[:length])+("missing",), seen=frozenset(f.items[:length]),
                               timestamp_ms=timestamp)
               for length, timestamp in ((0, 1), (1, 8), (3, 24), (8, 100), (12, 100))]
    predictor = MultiInterestPredictor(f, catalog, fallback, device="cpu", batch_size=batch_size)
    actual = predictor.rank_many(queries, k=5)
    assert [r.items for r in actual] == [oracle(f, catalog, fallback, q, 5) for q in queries]
    assert predictor.last_cost["dense_vector_dot_products"] >= 0


def test_recent_limit_and_missing_vectors_keep_original_distances():
    f, _, _ = fixture()
    history = (f.items[0],)*10 + ("missing", f.items[4], f.items[1])
    values = recent_vectors(f, [history], 3)[0]
    np.testing.assert_array_equal(values[0], f.vectors[1])
    assert not values[1:].any()


def test_source_is_label_independent_and_equal_timestamp_items_are_excluded():
    f, catalog, fallback = fixture()
    q = Query("q", 10, f.items[7], (f.items[0],), frozenset({f.items[0]}), True, True)
    modified = replace(q, target=f.items[9], target_available=False, target_model_cold=False)
    predictor = MultiInterestPredictor(f, catalog, fallback, device="cpu")
    first, second = predictor.rank_many([q, modified], k=20)
    assert first.items == second.items
    assert f.items[9] not in first.items and f.items[0] not in first.items
    assert all(catalog[item] < 10 for item in first.items)


def test_empty_or_unrepresented_history_uses_exact_fallback():
    f, catalog, fallback = fixture()
    queries = [SimpleNamespace(history=h, seen=frozenset(), timestamp_ms=100)
               for h in ((), ("missing",), (f.items[4],))]
    predictor = MultiInterestPredictor(f, catalog, fallback, device="cpu")
    assert [r.items for r in predictor.rank_many(queries, k=5)] == [f.items[:5]]*3


def test_negative_cosine_is_clamped_with_lexical_ties():
    f = ContentFeatures(("a", "b", "c"), np.array([[1.,0.],[-1.,0.],[-1.,0.]], dtype=np.float32))
    q = SimpleNamespace(history=("a",), seen={"a"}, timestamp_ms=2)
    ranks = MultiInterestPredictor(f, dict.fromkeys(f.items, 1), Fallback(f.items), device="cpu").rank_many([q], k=2)
    assert ranks[0].items == ("b", "c")


def test_content_fusion_budget_deduplication_and_rrf_oracle():
    left = [Ranking(tuple(f"x{i:03}" for i in range(200)))]
    right = [Ranking(tuple(f"x{i:03}" for i in range(100, 300)))]
    scores = {}
    for values in (left[0].items, right[0].items):
        for rank, item in enumerate(values, 1):
            scores[item] = scores.get(item, 0) + .5/(60+rank)
    expected = tuple(sorted(scores, key=lambda item: (-scores[item], item))[:200])
    actual = merge_content(left, right)[0].items
    assert actual == expected and len(actual) == len(set(actual)) == 200


@pytest.mark.parametrize("recent,decay", [(0,.9), (8,0), (8,1.1)])
def test_invalid_interest_settings_are_rejected(recent, decay):
    f, catalog, fallback = fixture()
    with pytest.raises(ValueError):
        MultiInterestPredictor(f, catalog, fallback, recent_events=recent, interest_decay=decay, device="cpu")


def point(name, ndcg, recall):
    return {"name": name, "metrics": {"cohorts": {"all_positive_events": {"ndcg@10": ndcg},
                                                  "model_cold_available": {"recall@20": recall}}}}


def test_selection_quality_floor_and_stable_ties():
    config = {"selection": {"floor_baselines": ["CF-blend", "A-frozen-s17"], "ndcg_min_ratio": .97,
                             "declaration_order": ["CF-blend", "A-frozen-s17", "C-adapted-s17", "D-adapted-s17"]}}
    rows = [point("CF-blend", .1, .02), point("A-frozen-s17", .12, .03),
            point("C-adapted-s17", .1, .9), point("D-adapted-s17", .12, .03)]
    chosen = select_method(rows, config)
    assert chosen["selected_method"] == "A-frozen-s17"
    assert "C-adapted-s17" not in chosen["eligible_methods"]
    assert chosen["validation_ndcg_floor"] == pytest.approx(.1164)


def test_test_access_rejects_incomplete_and_duplicate_trials():
    class Sealed:
        def queries(self, split, test_authorized=False):
            assert split == "test" and test_authorized
            return ["authorized"]
    valid = {"trials": [{"arm": arm, "seed": seed, "status": "completed", "checkpoint_reload_verified": True}
                        for arm in ("C", "D") for seed in (17,29,43)],
             "validation_results": [point(name, .1, .1) for name in
                 ["CF-blend", "A-RRF", "B-RRF"] +
                 [f"{arm}-{kind}-s{seed}" for arm, kind in (("A","frozen"),("B","frozen"),("C","adapted"),("D","adapted"))
                  for seed in (17,29,43)]],
             "selection_finished_at": "2026-09-17", "selected_method": "A-frozen-s17"}
    assert open_test(valid, Sealed()) == ["authorized"]
    for altered in ({**valid, "trials": valid["trials"][:-1]},
                    {**valid, "trials": [valid["trials"][0]]*6},
                    {**valid, "selection_finished_at": None},
                    {**valid, "validation_results": valid["validation_results"][:-1]}):
        with pytest.raises(ValueError):
            open_test(altered, Sealed())


def test_cold_subgroups_partition_only_available_cold_targets():
    queries = [Query(str(i),10,"x",h,frozenset(),available,cold)
               for i,(h,available,cold) in enumerate((((),True,True),(("a",),True,True),((),False,True),(("b",),True,False)))]
    masks = group_masks(queries)
    np.testing.assert_array_equal(masks["history_cold_available"] | masks["no_history_cold_available"],
                                  masks["model_cold_available"])
    assert not (masks["history_cold_available"] & masks["no_history_cold_available"]).any()
