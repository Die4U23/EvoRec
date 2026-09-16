"""Validation-selected temporal priors and bounded collaborative fusion."""

import math
from collections import Counter

from evorec.research.baselines import Ranking


class RecentPopular:
    def __init__(self, half_life_days=365):
        self.half_life_days = half_life_days
        self.name = f"RecentPopular-{half_life_days}d"

    def fit(self, events, cutoff, positive_min=4):
        self.counts = Counter()
        for event in events:
            if event.rating >= positive_min:
                age_days = (cutoff - event.timestamp_ms) / 86400000
                if age_days < 0:
                    raise ValueError("training event is after cutoff")
                self.counts[event.item_id] += 2 ** (-age_days / self.half_life_days)
        self.ordered = tuple(sorted(self.counts, key=lambda item: (-self.counts[item], item)))
        return self

    def rank(self, history, seen, available, k=200):
        result = []
        for item in self.ordered:
            if item in available and item not in seen:
                result.append(item)
                if len(result) >= k:
                    break
        return Ranking(tuple(result))


class CollaborativeBlend:
    def __init__(self, core, prior, alpha=.25, history_decay=.8):
        self.core, self.prior, self.alpha, self.history_decay = core, prior, alpha, history_decay
        self.name = f"CF-blend-a{alpha}"
        maximum = max(prior.counts.values())
        self.priors = {item: math.log1p(value) / math.log1p(maximum) for item, value in prior.counts.items()}

    def rank(self, history, seen, available, k=200):
        scores = Counter()
        for distance, item in enumerate(reversed(history)):
            for other, similarity in self.core.neighbors.get(item, ()):
                if other in available and other not in seen:
                    scores[other] += similarity * self.history_decay ** distance
        scale = max(scores.values(), default=1)
        candidates = set(scores)
        candidates.update(item for item in self.prior.ordered[:1000] if item in available and item not in seen)
        result = sorted(
            candidates,
            key=lambda item: (-(self.alpha * scores[item] / scale + (1 - self.alpha) * self.priors.get(item, 0)), item),
        )[:k]
        selected = set(result)
        for item in self.prior.ordered:
            if len(result) >= k:
                break
            if item not in selected and item in available and item not in seen:
                result.append(item)
                selected.add(item)
        personalized = sum(scores[item] > 0 for item in result)
        return Ranking(tuple(result), personalized, len(result) - personalized)
