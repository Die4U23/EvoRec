"""Prequential, single-positive evaluation with atomic equal-timestamp batches."""

import hashlib
import json
import math
import time
from collections import Counter, defaultdict, deque
from itertools import groupby

from evorec.research.data import split_name

GROUPS = ("all_positive_events", "available_target", "history_present", "cold_user", "model_cold_available")


def ranking_metrics(items: tuple[str, ...], target: str) -> dict[str, float]:
    if len(set(items)) != len(items):
        raise ValueError("duplicate recommendations distort ranking metrics")
    rank = items.index(target) + 1 if target in items else None
    return {
        "ndcg@10": 1 / math.log2(rank + 1) if rank is not None and rank <= 10 else 0.0,
        "recall@20": float(rank is not None and rank <= 20),
        "candidate_recall@200": float(rank is not None and rank <= 200),
    }


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


class Aggregate:
    def __init__(self):
        self.count = 0
        self.sums = Counter()
        self.recommended = set()
        self.catalog_size = 0
        self.latencies = []
        self.candidate_count = 0
        self.personalized_count = 0
        self.popularity_fill = 0

    def add(self, metrics, ranking, catalog_size, latency_ms):
        self.count += 1
        self.sums.update(metrics)
        self.recommended.update(ranking.items[:10])
        # Availability grows monotonically; max size equals the union over query times.
        self.catalog_size = max(self.catalog_size, catalog_size)
        self.latencies.append(latency_ms)
        self.candidate_count += len(ranking.items)
        self.personalized_count += ranking.personalized_count
        self.popularity_fill += ranking.popularity_fill

    def report(self):
        return {
            "n": self.count,
            **{key: self.sums[key] / self.count if self.count else None
               for key in ("ndcg@10", "recall@20", "candidate_recall@200")},
            "catalog_coverage@10": len(self.recommended) / self.catalog_size if self.catalog_size else None,
            "coverage_catalog_items": self.catalog_size,
            "unique_recommended_items@10": len(self.recommended),
            "mean_candidates": self.candidate_count / self.count if self.count else None,
            "mean_personalized_candidates": self.personalized_count / self.count if self.count else None,
            "mean_popularity_fill": self.popularity_fill / self.count if self.count else None,
            "ranking_latency_ms_p50": percentile(self.latencies, .50),
            "ranking_latency_ms_p95": percentile(self.latencies, .95),
        }


def evaluate(events, models, train_end_ms, validation_end_ms, history_limit=20,
             candidate_k=200, positive_min=4.0, request_output=None):
    if type(history_limit) is not int or history_limit < 1 or type(candidate_k) is not int or candidate_k < 200:
        raise ValueError("history limit must be positive and candidate K at least 200")
    split_name(0, train_end_ms, validation_end_ms)
    aggregates = {
        split: {model.name: {group: Aggregate() for group in GROUPS} for model in models}
        for split in ("validation", "test")
    }
    train_seen_items = {e.item_id for e in events if e.timestamp_ms < train_end_ms}
    histories = defaultdict(lambda: deque(maxlen=history_limit))
    seen = defaultdict(set)
    available = set()
    diagnostics = {split: Counter() for split in ("train", "validation", "test")}
    ordered = sorted(events, key=lambda e: (e.timestamp_ms, e.user_id, e.item_id))
    for timestamp, iterator in groupby(ordered, key=lambda e: e.timestamp_ms):
        batch = list(iterator)
        split = split_name(timestamp, train_end_ms, validation_end_ms)
        for event in batch:
            diagnostic = diagnostics[split]
            diagnostic["events"] += 1
            if event.rating < positive_min:
                diagnostic["non_positive_events"] += 1
                continue
            diagnostic["positive_events"] += 1
            if split == "train":
                continue
            history = tuple(histories[event.user_id])
            target_available = event.item_id in available
            target_cold = event.item_id not in train_seen_items
            diagnostic["target_unavailable_at_request"] += not target_available
            diagnostic["model_cold_available"] += target_cold and target_available
            diagnostic["cold_user_events"] += not bool(history)
            groups = ["all_positive_events", "history_present" if history else "cold_user"]
            if target_available:
                groups.append("available_target")
            if target_available and target_cold:
                groups.append("model_cold_available")
            for model in models:
                started = time.perf_counter()
                ranking = model.rank(history, seen[event.user_id], available, candidate_k)
                latency_ms = (time.perf_counter() - started) * 1000
                if any(item not in available or item in seen[event.user_id] for item in ranking.items):
                    raise ValueError("model returned an unavailable or previously seen item")
                if len(ranking.items) > candidate_k:
                    raise ValueError("model exceeded candidate budget")
                metrics = ranking_metrics(ranking.items, event.item_id)
                for group in groups:
                    aggregates[split][model.name][group].add(metrics, ranking, len(available), latency_ms)
                if request_output is not None:
                    identity = f"{event.user_id}\0{event.item_id}\0{timestamp}".encode()
                    record = {
                        "query_id": hashlib.sha256(identity).hexdigest()[:24],
                        "split": split, "timestamp_ms": timestamp, "method": model.name,
                        "target_item": event.item_id, "history": history,
                        "seen_item_count": len(seen[event.user_id]),
                        "target_available": target_available, "target_model_cold": target_cold,
                        "recommendations": ranking.items, "metrics": metrics,
                        "personalized_count": ranking.personalized_count,
                        "popularity_fill": ranking.popularity_fill,
                        "ranking_latency_ms": latency_ms,
                    }
                    request_output.write(json.dumps(record, separators=(",", ":")) + "\n")
        # No event at t can observe another event at t, including another user's item introduction.
        for event in batch:
            available.add(event.item_id)
            seen[event.user_id].add(event.item_id)
            if event.rating >= positive_min:
                histories[event.user_id].append(event.item_id)
    return {
        "diagnostics": {split: dict(counts) for split, counts in diagnostics.items()},
        "metrics": {split: {name: {group: agg.report() for group, agg in groups.items()}
                    for name, groups in methods.items()} for split, methods in aggregates.items()},
    }
