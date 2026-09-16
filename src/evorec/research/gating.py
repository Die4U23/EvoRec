"""Label-free request signals and deterministic cold-item reservation."""
from dataclasses import dataclass
from math import sqrt

from evorec.research.baselines import Ranking


@dataclass(frozen=True)
class HistorySignal:
    represented_count: int
    coherence: float


def history_signal(features, history, decay=.8):
    if not 0 < decay <= 1:
        raise ValueError("history decay must be in (0, 1]")
    total = [0.0] * features.vectors.shape[1]
    weight_sum, count = 0.0, 0
    for distance, item in enumerate(reversed(history)):
        index = features.mapping.get(item)
        if index is None or not features.present[index]:
            continue
        weight = decay**distance
        for j, value in enumerate(features.vectors[index]):
            total[j] += weight*float(value)
        weight_sum += weight
        count += 1
    coherence = sqrt(sum(v*v for v in total))/weight_sum if weight_sum else 0.0
    return HistorySignal(count, min(1.0, max(0.0, coherence)))


def reserve_cold(base, content, training_items, quota, *, top_n=20, positions=(10, 20), k=200):
    if quota not in (0, 1, 2) or top_n > k or k < 1 or top_n < 1:
        raise ValueError("invalid quota or ranking budget")
    if len(positions) < quota or any(p < 1 or p > top_n for p in positions):
        raise ValueError("invalid reservation positions")
    if len(set(base.items)) != len(base.items) or len(set(content.items)) != len(content.items):
        raise ValueError("duplicate input candidates")
    original = tuple(base.items[:k])
    existing = [item for item in original[:top_n] if item not in training_items]
    if quota == 0 or len(existing) >= quota:
        return Ranking(original), 0
    # Keep all previously exposed cold items, so insertion cannot eject one at rank 20.
    chosen = list(existing)
    chosen.extend(item for item in content.items if item not in training_items and item not in chosen)
    chosen = chosen[:quota]
    if len(chosen) <= len(existing):
        return Ranking(original), 0
    remaining = [item for item in original if item not in chosen]
    for item in content.items:
        if item not in chosen and item not in remaining:
            remaining.append(item)
    slots = positions[-len(chosen):]
    result = remaining[:k]
    for position, item in zip(slots, chosen, strict=True):
        result.insert(min(position-1, len(result)), item)
    return Ranking(tuple(result[:k])), len(chosen)-len(existing)


def apply_policy(base, content, signals, training_items, policy, gate):
    if not (len(base) == len(content) == len(signals)):
        raise ValueError("inconsistent request count")
    outputs, records = [], []
    for original, raw, signal in zip(base, content, signals, strict=True):
        active = (not policy["gated"] or
                  signal.represented_count >= gate["min_history"] and signal.coherence >= gate["min_coherence"])
        if active:
            ranking, promoted = reserve_cold(original, raw, training_items, policy["quota"],
                                             top_n=gate["top_n"], positions=tuple(gate["positions"]))
        else:
            ranking, promoted = original, 0
        outputs.append(ranking)
        records.append({"represented_history": signal.represented_count, "coherence": signal.coherence,
                        "gate_passed": active, "new_cold_promoted": promoted, "ranking_changed": ranking.items != original.items})
    return outputs, records


def select_policy(rows, minimum_ratio=.97):
    baseline = next(row for row in rows if row["policy"]["name"] == "fixed")
    floor = minimum_ratio*baseline["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"]
    eligible = [row for row in rows if row["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"] >= floor]
    if not eligible:
        raise ValueError("missing eligible baseline")
    chosen = max(eligible, key=lambda row: (
        row["metrics"]["cohorts"]["model_cold_available"]["recall@20"],
        row["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"], -row["policy"]["quota"]))
    return chosen["policy"], floor
