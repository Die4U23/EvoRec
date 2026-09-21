"""Target-independent recent-interest retrieval with the R05 candidate contract."""
import time

import numpy as np
import torch

from evorec.research.baselines import Ranking
from evorec.research.content import ContentPredictor, reciprocal_fusion
from evorec.research.protocol import AvailableAt


def recent_vectors(features, histories, recent_events=8):
    if type(recent_events) is not int or recent_events < 1:
        raise ValueError("recent_events must be positive")
    vectors = np.zeros((len(histories), recent_events, features.vectors.shape[1]), dtype=np.float32)
    for row, history in enumerate(histories):
        for distance, item in enumerate(reversed(history[-recent_events:])):
            index = features.mapping.get(item)
            if index is not None and features.present[index]:
                vectors[row, distance] = features.vectors[index]
    return vectors


class MultiInterestPredictor(ContentPredictor):
    def __init__(self, *args, recent_events=8, interest_decay=.9, **kwargs):
        super().__init__(*args, **kwargs)
        if self.model is not None or not 0 < interest_decay <= 1:
            raise ValueError("expected raw vectors and decay in (0, 1]")
        if type(recent_events) is not int or recent_events < 1:
            raise ValueError("recent_events must be positive")
        self.recent_events, self.interest_decay = recent_events, interest_decay
        self.last_cost = {}

    @torch.inference_mode()
    def rank_many(self, queries, k=200):
        if type(k) is not int or k < 1:
            raise ValueError("k must be positive")
        if self.device.startswith("cuda"):
            torch.cuda.synchronize()
        started = time.perf_counter()
        vectors = recent_vectors(self.features, [q.history for q in queries], self.recent_events)
        results, dot_products = [], 0
        for offset in range(0, len(queries), self.batch_size):
            batch = queries[offset:offset + self.batch_size]
            values = torch.from_numpy(vectors[offset:offset + len(batch)]).to(self.device)
            effective = values.norm(dim=-1).gt(1e-8).any(dim=1)
            scores = torch.zeros((len(batch), len(self.features.items)), device=self.device)
            for distance in range(self.recent_events):
                if not bool(values[:, distance].norm(dim=1).gt(1e-8).any()):
                    continue
                similarities = values[:, distance] @ self.raw_items.T
                if not torch.isfinite(similarities).all():
                    raise ValueError("non-finite interest scores")
                scores = torch.maximum(scores, similarities.clamp_min(0) * self.interest_decay**distance)
                dot_products += len(batch) * len(self.features.items)
            timestamps = torch.tensor([q.timestamp_ms for q in batch], device=self.device)
            scores.masked_fill_(self.first_seen[None, :] >= timestamps[:, None], -torch.inf)
            scores.masked_fill_(~self.present[None, :], -torch.inf)
            for row, query in enumerate(batch):
                columns = [self.features.mapping[item] for item in query.seen if item in self.features.mapping]
                if columns:
                    scores[row, columns] = -torch.inf
            indices = torch.argsort(scores, dim=1, descending=True, stable=True)[:, :min(k, len(self.features.items))]
            finite = torch.gather(scores, 1, indices).isfinite().cpu().tolist()
            for row, selected in enumerate(indices.cpu().tolist()):
                query = batch[row]
                fallback = self.fallback.rank((), query.seen, AvailableAt(self.catalog, query.timestamp_ms), k)
                if not bool(effective[row]):
                    results.append(Ranking(fallback.items, 0, len(fallback.items)))
                    continue
                items = [self.features.items[i] for i, valid in zip(selected, finite[row], strict=True) if valid]
                count = len(items)
                items.extend(item for item in fallback.items if item not in items)
                items = items[:k]
                results.append(Ranking(tuple(items), count, len(items)-count))
        if self.device.startswith("cuda"):
            torch.cuda.synchronize()
        self.last_cost = {"wall_seconds": time.perf_counter()-started,
                          "dense_vector_dot_products": dot_products,
                          "queries": len(queries), "catalog_items": len(self.features.items)}
        return results


def merge_content(centroid, interests, *, k=200, weight=.5, constant=60):
    """Keep exactly one content budget; no labels enter fusion."""
    return reciprocal_fusion(centroid, interests, alpha=1-weight, constant=constant, k=k)
