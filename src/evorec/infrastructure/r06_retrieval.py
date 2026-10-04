"""Bounded, immutable CF/centroid retrieval over a controlled frozen snapshot.

Standard library only. No fitting, executable models, live catalog adaptation or
activation. Content scores use explicit float32 products and sequential float32
accumulation: different BLAS/GPU reductions can reorder near-equal scores.
"""

from collections import Counter
from dataclasses import dataclass
import hashlib
import heapq
from itertools import islice
import math
from pathlib import Path
import struct
from types import MappingProxyType
from typing import Mapping

from evorec.infrastructure.r06_features import (
    PROVENANCE_HASHES, MAX_ITEMS, MAX_JSON_BYTES, R06Features, _ids, _json, _timestamp,
    validate_request,
)
from evorec.infrastructure.residual_ranker import ControlledLoadError, _digest, _f32, _integer, _object, _read, _verified

MAX_EDGES = 2_000_000
MAX_GRAPH_BYTES = 32 * 1024 * 1024
BINDING_KEYS = (*PROVENANCE_HASHES, "selection_protocol_id", "training_protocol_id")


def retrieval_protocol():
    return {"history_decay": .8, "history_limit": 50, "provider_k": 200,
            "cf_alpha": .25, "prior_pool": 1000, "half_life_days": 365,
            "max_user_items": 100, "neighbors_per_item": 100,
            "content_arithmetic": "f32-product-sequential-f32-sum-v1"}


def _content_score(context, vector):
    score = 0.
    for a, b in zip(context, vector, strict=True):
        score = _f32(score + _f32(a * b))
    return score


def _fail(code, message):
    raise ControlledLoadError(code, message)


@dataclass(frozen=True)
class RetrievalResult:
    collaborative: tuple[str, ...]
    content: tuple[str, ...]
    collaborative_personalized: int
    content_personalized: int


@dataclass(frozen=True)
class R06Retrieval:
    manifest_sha256: str
    validation_samples_checked: int
    edge_count: int
    provenance: Mapping[str, str]
    _features: R06Features
    _ordered: tuple[int, ...]
    _priors: tuple[float, ...]
    _offsets: tuple[int, ...]
    _edges: bytes

    def _available(self, index, seen, timestamp):
        return (self._features._metadata[index].first_seen_ms < timestamp
                and self._features.item_ids[index] not in seen)

    def retrieve(self, history, seen, timestamp_ms):
        history, seen, timestamp_ms = validate_request(history, seen, timestamp_ms)
        features = self._features
        if any(features._metadata[features._indices[item]].first_seen_ms >= timestamp_ms
               for item in history if item in features._indices):
            _fail("input_shape", "known history item is not strictly available at the request time")
        fallback = tuple(islice((index for index in self._ordered if self._available(index, seen, timestamp_ms)), 200))
        scores = Counter()
        for distance, item in enumerate(reversed(history)):
            index = features._indices.get(item)
            if index is None:
                continue
            for edge in range(self._offsets[index], self._offsets[index + 1]):
                other, similarity = struct.unpack_from("<Id", self._edges, edge * 12)
                if self._available(other, seen, timestamp_ms):
                    scores[other] += similarity * .8 ** distance
        scale = max(scores.values(), default=1.)
        candidates = set(scores)
        # Original protocol filters AFTER truncating the prior to 1000, not before.
        candidates.update(i for i in self._ordered[:1000] if self._available(i, seen, timestamp_ms))
        cf = sorted(candidates, key=lambda i: (-(.25 * scores[i] / scale + .75 * self._priors[i]), i))[:200]
        selected = set(cf)
        for index in fallback:
            if len(cf) >= 200:
                break
            if index not in selected:
                cf.append(index)
                selected.add(index)
        context, _ = features._history(history)
        content = []
        if math.sqrt(sum(value * value for value in context)) > 1e-8:
            def ranked_items():
                for index, item in enumerate(features.item_ids):
                    if features._present[index] and self._available(index, seen, timestamp_ms):
                        # No rounding/epsilon buckets: ties use sorted item IDs.
                        score = _content_score(context, features.vector(item))
                        yield -score, index
            content = [i for _, i in heapq.nsmallest(200, ranked_items())]
        content_personalized = len(content)
        selected = set(content)
        for index in fallback:
            if len(content) >= 200:
                break
            if index not in selected:
                content.append(index)
                selected.add(index)
        return RetrievalResult(tuple(features.item_ids[i] for i in cf),
                               tuple(features.item_ids[i] for i in content),
                               sum(scores[i] > 0 for i in cf), content_personalized)

    def build_pool(self, history, seen, timestamp_ms):
        result = self.retrieve(history, seen, timestamp_ms)
        return self._features.build_pool(history, seen, timestamp_ms, result.collaborative, result.content)


def load_r06_retrieval(component_dir, features, *, expected_manifest_sha256=None):
    if type(features) is not R06Features:
        _fail("component_schema", "a controlled frozen feature component is required")
    supplied = Path(component_dir)
    if supplied.is_symlink():
        _fail("unsafe_path", "retrieval directory cannot be a symlink")
    root = supplied.resolve()
    try:
        names = set()
        for path in root.iterdir():
            names.add(path.name)
            if len(names) > 4:
                break
    except OSError as error:
        raise ControlledLoadError("component_unreadable", "cannot read retrieval directory") from error
    if names != {"manifest.json", "statistics.json", "neighbors.bin", "validation.json"}:
        _fail("unsupported_format", "retrieval directory must contain exactly four controlled files")
    raw = _read(root, "manifest.json", MAX_JSON_BYTES)
    digest = hashlib.sha256(raw).hexdigest()
    if expected_manifest_sha256 is not None and digest != _digest(expected_manifest_sha256):
        _fail("component_changed", "retrieval manifest differs from the approved digest")
    manifest = _object(_json(raw), ("schema_version", "kind", "graph_dtype", "item_count", "edge_count",
                                  "protocol", "fit", "provenance", "statistics", "neighbors", "validation"))
    protocol = retrieval_protocol()
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["kind"] != "r06-cf-centroid-v1" or manifest["graph_dtype"] != "csr-u32-f64-le"
            or not isinstance(manifest["protocol"], dict) or set(manifest["protocol"]) != set(protocol)
            or any(type(manifest["protocol"][k]) is not type(v) or manifest["protocol"][k] != v
                   for k, v in protocol.items())):
        _fail("component_schema", "unsupported frozen retrieval protocol")
    count = _integer(manifest["item_count"], MAX_ITEMS)
    edges = manifest["edge_count"]
    if type(edges) is not int or not 0 <= edges <= min(MAX_EDGES, 100 * count):
        _fail("resource_limit", "neighbor edge count exceeds supported limits")
    fit = _object(manifest["fit"], ("end_ms", "training_rows", "positive_rating_min"))
    _timestamp(fit["end_ms"])
    _integer(fit["training_rows"], 10_000_000)
    if type(fit["positive_rating_min"]) is not int or fit["positive_rating_min"] != 4:
        _fail("component_schema", "training positive threshold differs")
    provenance = _object(manifest["provenance"], (*BINDING_KEYS, "features_manifest_sha256", "selected_method"))
    for key in (*PROVENANCE_HASHES, "features_manifest_sha256"):
        _digest(provenance[key])
    for key in ("selection_protocol_id", "training_protocol_id"):
        _digest(provenance[key], 16)
    if (count != len(features.item_ids) or provenance["selected_method"] != "A-frozen-s17"
            or provenance["features_manifest_sha256"] != features.manifest_sha256
            or any(provenance[key] != features.provenance[key] for key in BINDING_KEYS)):
        _fail("component_changed", "retrieval and frozen feature identities differ")
    counts = _json(_verified(root, manifest["statistics"], "statistics.json", MAX_JSON_BYTES))
    if (not isinstance(counts, list) or len(counts) != count
            or any(type(v) not in (int, float) or not 0 <= v <= fit["training_rows"] or not math.isfinite(v) for v in counts)):
        _fail("input_shape", "invalid temporal positive counts")
    maximum = max(counts)
    if maximum <= 0:
        _fail("input_shape", "at least one positive training prior is required")
    priors = tuple(math.log1p(value) / math.log1p(maximum) for value in counts)
    if any(abs(actual - metadata.prior_strength) > 1e-12 or (counts[i] > 0 and
           (not metadata.training_item or metadata.first_seen_ms >= fit["end_ms"]))
           for i, (actual, metadata) in enumerate(zip(priors, features._metadata, strict=True))):
        _fail("component_changed", "temporal priors differ from the frozen feature statistics")
    ordered = tuple(sorted((i for i, value in enumerate(counts) if value > 0), key=lambda i: (-counts[i], i)))
    _object(manifest["neighbors"], ("path", "size_bytes", "sha256"))
    if manifest["neighbors"]["size_bytes"] != 4 * (count + 1) + 12 * edges:
        _fail("input_shape", "neighbor bytes differ from the declared graph shape")
    graph = _verified(root, manifest["neighbors"], "neighbors.bin", MAX_GRAPH_BYTES)
    offsets = struct.unpack_from(f"<{count + 1}I", graph)
    edge_bytes = graph[4 * (count + 1):]
    if offsets[0] != 0 or offsets[-1] != edges:
        _fail("input_shape", "invalid graph offset endpoints")
    for anchor in range(count):
        start, end = offsets[anchor:anchor + 2]
        if not 0 <= start <= end <= edges or end - start > 100 or (end > start and counts[anchor] <= 0):
            _fail("input_shape", "invalid bounded neighbor row")
        previous, seen = None, set()
        for edge in range(start, end):
            index, similarity = struct.unpack_from("<Id", edge_bytes, edge * 12)
            if (index >= count or index == anchor or index in seen or counts[index] <= 0
                    or not math.isfinite(similarity) or not 1 / fit["training_rows"] <= similarity <= 1):
                _fail("input_shape", "invalid finite neighbor edge")
            key = (-similarity, index)
            if previous is not None and key < previous:
                _fail("input_shape", "neighbor rows must use stable similarity/ID order")
            previous = key
            seen.add(index)
    samples = _json(_verified(root, manifest["validation"], "validation.json", MAX_JSON_BYTES))
    if not isinstance(samples, list) or not 2 <= len(samples) <= 4:
        _fail("resource_limit", "two to four full provider references are required")
    runtime = R06Retrieval(digest, len(samples), edges, MappingProxyType(dict(provenance)),
                           features, ordered, priors, offsets, edge_bytes)
    represented, empty = False, False
    for sample in samples:
        _object(sample, ("history", "seen", "timestamp_ms", "collaborative", "content"))
        history, exclusions, timestamp = validate_request(sample["history"], sample["seen"], sample["timestamp_ms"])
        expected_cf = _ids(sample["collaborative"], 200, unique=True)
        expected_content = _ids(sample["content"], 200, unique=True)
        result = runtime.retrieve(history, exclusions, timestamp)
        if result.collaborative != expected_cf or result.content != expected_content:
            _fail("validation_mismatch", "full provider order differs from the archived request")
        effective = math.sqrt(sum(v * v for v in features._history(history)[0])) > 1e-8
        represented |= effective and result.content_personalized > 0
        empty |= not effective and bool(result.content)
    if not represented or not empty:
        _fail("validation_mismatch", "represented and cold fallback references are required")
    return runtime
