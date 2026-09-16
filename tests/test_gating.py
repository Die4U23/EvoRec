from types import SimpleNamespace
import pytest
from evorec.research.baselines import Ranking
from evorec.research.gating import HistorySignal, history_signal, reserve_cold, apply_policy, select_policy


class Vectors:
    shape = (3, 2)
    def __getitem__(self, index):
        return [[1, 0], [-1, 0], [0, 0]][index]


def test_history_signal_uses_only_representable_history():
    features = SimpleNamespace(vectors=Vectors(), mapping={"a": 0, "b": 1, "missing": 2}, present=[True, True, False])
    assert history_signal(features, ()) == HistorySignal(0, 0)
    assert history_signal(features, ("unknown", "a")).represented_count == 1
    assert history_signal(features, ("a", "b"), decay=1).coherence == 0
    assert history_signal(features, ("a", "missing", "a")).coherence == 1
    with pytest.raises(ValueError):
        history_signal(features, ("a",), decay=0)


def test_single_slot_keeps_top_ten_and_retrieves_one_cold_item():
    base = Ranking(tuple(f"w{i}" for i in range(30)))
    output, count = reserve_cold(base, Ranking(("c",)), set(base.items), 1)
    assert output.items[:10] == base.items[:10]
    assert output.items[19] == "c"
    assert count == 1 and len(set(output.items)) == len(output.items)


def test_existing_cold_at_rank_twenty_is_preserved_when_adding_second():
    base = Ranking(tuple([f"w{i}" for i in range(19)] + ["old-cold"] + ["tail"]))
    training = {item for item in base.items if item != "old-cold"}
    output, count = reserve_cold(base, Ranking(("new-cold",)), training, 2)
    assert {"old-cold", "new-cold"} <= set(output.items[:20])
    assert output.items[9] == "old-cold" and output.items[19] == "new-cold"
    assert count == 1


def test_already_satisfied_quota_is_noop():
    base = Ranking(("c1", "c2", "w"))
    output, count = reserve_cold(base, Ranking(("c3",)), {"w"}, 2)
    assert output.items == base.items and count == 0


def test_no_cold_candidate_does_not_invent_fill():
    base = Ranking(("a", "b"))
    output, count = reserve_cold(base, Ranking(("b", "c")), {"a", "b", "c"}, 2)
    assert output.items == base.items and count == 0


def test_gate_blocks_short_or_incoherent_histories():
    base = [Ranking(tuple(f"w{i}" for i in range(20)))] * 3
    raw = [Ranking(("cold",))] * 3
    signals = [HistorySignal(1, 1), HistorySignal(3, .59), HistorySignal(2, .6)]
    output, audit = apply_policy(base, raw, signals, set(base[0].items),
                                {"name": "gated-1", "quota": 1, "gated": True},
                                {"min_history": 2, "min_coherence": .6, "top_n": 20, "positions": [10, 20]})
    assert [row["ranking_changed"] for row in audit] == [False, False, True]
    assert output[2].items[19] == "cold"


def test_reject_invalid_inputs():
    with pytest.raises(ValueError):
        reserve_cold(Ranking(("x", "x")), Ranking(()), set(), 1)
    with pytest.raises(ValueError):
        reserve_cold(Ranking(()), Ranking(()), set(), 3)
    with pytest.raises(ValueError):
        apply_policy([], [Ranking(())], [], set(), {}, {})


def test_selection_respects_overall_quality_floor():
    def row(name, quota, ndcg, cold):
        return {"policy": {"name": name, "quota": quota, "gated": False},
                "metrics": {"cohorts": {"all_positive_events": {"ndcg@10": ndcg},
                                       "model_cold_available": {"recall@20": cold}}}}
    baseline = row("fixed", 0, .01, .001)
    eligible = row("reserve-1", 1, .0098, .003)
    failing = row("reserve-2", 2, .0095, .02)
    policy, floor = select_policy([baseline, eligible, failing], .97)
    assert policy["name"] == "reserve-1" and floor == pytest.approx(.0097)
    policy, _ = select_policy([baseline, row("reserve-1", 1, .01, .001)])
    assert policy["name"] == "fixed"
