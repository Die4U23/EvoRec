"""Frozen CPU baselines. All learned counts come from training positives only."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from math import sqrt

from evorec.research.data import Event


@dataclass(frozen=True)
class Ranking:
    items: tuple[str, ...]
    personalized_count: int = 0
    popularity_fill: int = 0


class Popular:
    name = "popular"

    def fit(self, events: list[Event], positive_min=4.0):
        pairs = {(e.user_id, e.item_id) for e in events if e.rating >= positive_min}
        self.counts = Counter(item for _, item in pairs)
        self.ordered = tuple(sorted(self.counts, key=lambda item: (-self.counts[item], item)))
        self.fit_stats = {"positive_user_item_pairs": len(pairs), "scored_items": len(self.ordered)}
        return self

    def rank(self, history, seen, available, k=200) -> Ranking:
        result = []
        for item in self.ordered:
            if item in available and item not in seen:
                result.append(item)
                if len(result) == k:
                    break
        return Ranking(tuple(result))


class ItemCF:
    name = "itemcf"

    def __init__(self, max_user_items=100, neighbors=100):
        if type(max_user_items) is not int or max_user_items < 2 or type(neighbors) is not int or neighbors < 1:
            raise ValueError("invalid ItemCF limits")
        self.max_user_items, self.neighbor_limit = max_user_items, neighbors

    def fit(self, events: list[Event], positive_min=4.0):
        self.popular = Popular().fit(events, positive_min)
        by_user = defaultdict(dict)
        for event in events:
            if event.rating >= positive_min:
                by_user[event.user_id][event.item_id] = event.timestamp_ms
        support, cooccurrence = Counter(), defaultdict(Counter)
        truncated = 0
        for items in by_user.values():
            ordered = sorted(items, key=lambda item: (items[item], item))
            truncated += len(ordered) > self.max_user_items
            basket = ordered[-self.max_user_items:]
            support.update(basket)
            for left, right in combinations(basket, 2):
                cooccurrence[left][right] += 1
                cooccurrence[right][left] += 1
        self.neighbors = {}
        for item, counts in cooccurrence.items():
            scores = [(other, count / sqrt(support[item] * support[other])) for other, count in counts.items()]
            self.neighbors[item] = tuple(sorted(scores, key=lambda pair: (-pair[1], pair[0]))[:self.neighbor_limit])
        self.fit_stats = {
            **self.popular.fit_stats, "users_truncated": truncated,
            "max_user_items": self.max_user_items, "neighbors_per_item": self.neighbor_limit,
            "retained_neighbor_edges": sum(map(len, self.neighbors.values())),
        }
        return self

    def rank(self, history, seen, available, k=200) -> Ranking:
        scores = Counter()
        for item in history:
            for other, similarity in self.neighbors.get(item, ()):
                if other in available and other not in seen:
                    scores[other] += similarity
        result = sorted(scores, key=lambda item: (-scores[item], item))[:k]
        personalized_count = len(result)
        selected = set(result)
        # Explicit lexicographic policy: positive CF scores first, then popularity.
        # Never add raw popularity counts to cosine similarities.
        for item in self.popular.ordered:
            if len(result) >= k:
                break
            if item in available and item not in seen and item not in selected:
                result.append(item)
                selected.add(item)
        return Ranking(tuple(result), personalized_count, len(result) - personalized_count)
