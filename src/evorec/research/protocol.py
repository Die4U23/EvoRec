"""R02 query snapshots and a sealed test split for validation-only model selection."""

import hashlib
import json
from bisect import bisect_left
from collections import defaultdict, deque
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path

from evorec.research.baselines import Ranking
from evorec.research.data import load_events
from evorec.research.evaluation import Aggregate, GROUPS, ranking_metrics
from evorec.research.runner import file_sha


@dataclass(frozen=True)
class Query:
    query_id: str
    timestamp_ms: int
    target: str
    history: tuple[str, ...]
    seen: frozenset[str]
    target_available: bool
    target_model_cold: bool


class AvailableAt:
    def __init__(self, first_seen, timestamp):
        self.first_seen, self.timestamp = first_seen, timestamp

    def __contains__(self, item):
        return self.first_seen.get(item, self.timestamp) < self.timestamp


class Protocol:
    def __init__(self, config):
        self.config = config
        path = Path(config["dataset_path"])
        self.manifest = json.loads(path.with_suffix(".manifest.json").read_text())
        if self.manifest["status"] != "completed_user_hash_sample" or file_sha(path) != self.manifest["sample_sha256"]:
            raise ValueError("R02 sample integrity failure")
        catalog_path = Path(self.manifest["catalog_path"])
        if file_sha(catalog_path) != self.manifest["catalog_sha256"]:
            raise ValueError("catalog integrity failure")
        self.catalog = json.loads(catalog_path.read_text())
        self.catalog_times = sorted(self.catalog.values())
        self.events, self.statistics = load_events(path)
        if self.statistics["input_rows"] != self.manifest["rows"]:
            raise ValueError("row count mismatch")
        if any(self.catalog.get(e.item_id, e.timestamp_ms + 1) > e.timestamp_ms for e in self.events):
            raise ValueError("sample and full catalog times disagree")
        self.train = [e for e in self.events if e.timestamp_ms < config["train_end_ms"]]
        self.train_items = {e.item_id for e in self.train}
        self.vocabulary = tuple(sorted({e.item_id for e in self.train if e.rating >= config["positive_rating_min"]}))
        fingerprint = {
            "sample_sha256": self.manifest["sample_sha256"],
            "catalog_sha256": self.manifest["catalog_sha256"],
            **{key: config[key] for key in ("train_end_ms", "validation_end_ms", "positive_rating_min", "history_limit")},
            "protocol": "r02-v1-strict-time-full-catalog-all-positive-events",
        }
        self.protocol_id = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:16]
        self.fingerprint = fingerprint

    def queries(self, split, *, test_authorized=False):
        if split not in {"validation", "test"}:
            raise ValueError("expected validation or test")
        if split == "test" and not test_authorized:
            raise ValueError("test is sealed until model selection is finished")
        start = self.config["train_end_ms"] if split == "validation" else self.config["validation_end_ms"]
        stop = self.config["validation_end_ms"] if split == "validation" else float("inf")
        histories = defaultdict(lambda: deque(maxlen=self.config["history_limit"]))
        seen = defaultdict(set)
        result = []
        for timestamp, iterator in groupby(self.events, key=lambda event: event.timestamp_ms):
            if timestamp >= stop:
                break
            batch = list(iterator)
            for event in batch:
                if timestamp >= start and event.rating >= self.config["positive_rating_min"]:
                    identity = f"{event.user_id}\0{event.item_id}\0{timestamp}".encode()
                    result.append(Query(
                        hashlib.sha256(identity).hexdigest()[:24], timestamp, event.item_id,
                        tuple(histories[event.user_id]), frozenset(seen[event.user_id]),
                        self.catalog[event.item_id] < timestamp, event.item_id not in self.train_items,
                    ))
            for event in batch:
                seen[event.user_id].add(event.item_id)
                if event.rating >= self.config["positive_rating_min"]:
                    histories[event.user_id].append(event.item_id)
        return result

    def training_examples(self):
        mapping = {item: index + 1 for index, item in enumerate(self.vocabulary)}
        histories = defaultdict(lambda: deque(maxlen=self.config["history_limit"]))
        examples = []
        for timestamp, iterator in groupby(self.train, key=lambda event: event.timestamp_ms):
            batch = [e for e in iterator if e.rating >= self.config["positive_rating_min"]]
            for event in batch:
                if histories[event.user_id]:
                    examples.append((tuple(histories[event.user_id]), mapping[event.item_id]))
            for event in batch:
                histories[event.user_id].append(mapping[event.item_id])
        return examples


def summarize(protocol, queries, rankings, latencies=None):
    if len(queries) != len(rankings):
        raise ValueError("query/result count differs")
    aggregates = {name: Aggregate() for name in GROUPS}
    unavailable = 0
    for index, (query, ranking) in enumerate(zip(queries, rankings, strict=True)):
        available = AvailableAt(protocol.catalog, query.timestamp_ms)
        if len(ranking.items) > 200 or any(item not in available or item in query.seen for item in ranking.items):
            raise ValueError("invalid candidates")
        groups = ["all_positive_events", "history_present" if query.history else "cold_user"]
        if query.target_available:
            groups.append("available_target")
        else:
            unavailable += 1
        if query.target_available and query.target_model_cold:
            groups.append("model_cold_available")
        metrics = ranking_metrics(ranking.items, query.target)
        catalog_size = bisect_left(protocol.catalog_times, query.timestamp_ms)
        for group in groups:
            aggregates[group].add(metrics, ranking, catalog_size, latencies[index] if latencies else 0)
    reports = {name: aggregate.report() for name, aggregate in aggregates.items()}
    if latencies is None:
        # Batched GPU timing cannot be represented as per-query latency percentiles.
        for report in reports.values():
            report["ranking_latency_ms_p50"] = None
            report["ranking_latency_ms_p95"] = None
    return {"cohorts": reports, "target_unavailable_events": unavailable}


def trace_records(queries, rankings):
    for query, ranking in zip(queries, rankings, strict=True):
        yield {
            "query_id": query.query_id, "timestamp_ms": query.timestamp_ms,
            "target": query.target, "history": query.history,
            "target_available": query.target_available, "target_model_cold": query.target_model_cold,
            "recommendations": ranking.items, "metrics": ranking_metrics(ranking.items, query.target),
        }
