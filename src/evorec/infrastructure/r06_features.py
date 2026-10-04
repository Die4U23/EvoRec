"""Frozen R06 item vectors and label-free pool features; no retrieval/publication.

This component accepts already retrieved candidates. It cannot encode new text,
invent candidates, or replace the active recommendation bundle.
"""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import struct
from types import MappingProxyType
from typing import Mapping, Sequence

from evorec.infrastructure.residual_ranker import (
    SCALAR_NAMES, ControlledLoadError, ResidualRanker,
    _digest, _f32, _integer, _json as _ranker_json, _object, _read, _verified,
)

MAX_ITEMS = 200_000
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_VECTOR_BYTES = 128 * 1024 * 1024
FEATURE_TOLERANCE = 1e-6
MAX_TIMESTAMP_MS = 253402300799999
PROVENANCE_HASHES = (
    "feature_fingerprint", "selection_series_sha256", "source_series_sha256",
    "training_sample_sha256", "catalog_sha256", "items_sha256", "vectors_sha256",
    "validation_pool_sha256", "validation_sources_sha256",
)
RANKER_BINDING_KEYS = (
    "feature_fingerprint", "selection_series_sha256", "source_series_sha256",
    "items_sha256", "vectors_sha256", "validation_pool_sha256",
    "selection_protocol_id", "training_protocol_id",
)


def _fail(code, message):
    raise ControlledLoadError(code, message)


def _json(raw):
    try:
        # CPython patch versions do not share the same decoder recursion limit.
        # Enforce the component contract before decoding nested containers,
        # ignoring brackets/braces inside strings and honoring escaped quotes.
        decoded = raw.decode("utf-8")
        depth, quoted, escaped = 0, False, False
        for character in decoded:
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character in "[{":
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    _fail("invalid_json", "JSON nesting exceeds the component limit")
            elif character in "]}":
                depth -= 1
        return _ranker_json(decoded)
    except ControlledLoadError:
        raise
    except (ValueError, RecursionError) as error:
        raise ControlledLoadError("invalid_json", "unsupported JSON literal or nesting") from error


def _timestamp(value):
    if type(value) is not int or not 0 <= value <= MAX_TIMESTAMP_MS:
        _fail("input_shape", "timestamp must be bounded epoch milliseconds")
    return value


def _ids(values, limit, *, unique=False):
    if (not isinstance(values, Sequence) or isinstance(values, (str, bytes))
            or len(values) > limit):
        _fail("input_shape", "invalid or oversized item sequence")
    result = tuple(values)
    if any(not isinstance(item, str) or not item.strip() or len(item) > 128 for item in result):
        _fail("input_shape", "invalid item ID")
    if unique and len(set(result)) != len(result):
        _fail("input_shape", "duplicate provider item")
    return result


@dataclass(frozen=True, slots=True)
class ItemFeatures:
    first_seen_ms: int
    training_item: bool
    prior_strength: float


@dataclass(frozen=True)
class PoolFeatures:
    item_ids: tuple[str, ...]
    context: tuple[float, ...]
    candidates: tuple[tuple[float, ...], ...]
    scalars: tuple[tuple[float, ...], ...]
    feature_fingerprint: str
    _ranker_binding: tuple[tuple[str, str], ...]

    def score(self, ranker: ResidualRanker):
        if (ranker.dimension != len(self.context)
                or any(ranker.provenance.get(key) != expected for key, expected in self._ranker_binding)):
            _fail("component_changed", "ranker and feature snapshot do not match")
        if not self.item_ids:
            return ()
        return ranker.score(self.context, self.candidates, self.scalars)


@dataclass(frozen=True)
class R06Features:
    dimension: int
    item_ids: tuple[str, ...]
    manifest_sha256: str
    feature_fingerprint: str
    validation_samples_checked: int
    provenance: Mapping[str, str]
    _indices: Mapping[str, int]
    _metadata: tuple[ItemFeatures, ...]
    _present: bytes
    _vectors: bytes

    def vector(self, item):
        try:
            index = self._indices[item]
        except (KeyError, TypeError) as error:
            raise ControlledLoadError("input_shape", "candidate is outside the frozen item snapshot") from error
        return struct.unpack_from(f"<{self.dimension}f", self._vectors, index * self.dimension * 4)

    def _history(self, history):
        context = [0.] * self.dimension
        signal = [0.] * self.dimension
        weight_sum = 0.
        for distance, item in enumerate(reversed(history)):
            index = self._indices.get(item)
            if index is None or not self._present[index]:
                continue
            weight = .8 ** distance  # Unknown/unrepresented events still occupy their distances.
            weight32 = _f32(weight)
            for column, value in enumerate(self.vector(item)):
                context[column] = _f32(context[column] + _f32(value * weight32))
                signal[column] += value * weight
            weight_sum += weight
        norm = _f32(math.sqrt(_f32(sum(value * value for value in context))))
        # sklearn's float32 normalization treats norms below 10*eps as unit scale.
        scale = 1. if norm < 10 * 2**-23 else norm
        context = tuple(_f32(value / scale) for value in context)
        coherence = math.sqrt(sum(value * value for value in signal)) / weight_sum if weight_sum else 0.
        return context, min(1., max(0., coherence))

    def build_pool(self, history, seen, timestamp_ms, collaborative, content):
        """Build the original sorted union and eight scalars, never target labels.

        Reject illegal candidates rather than silently altering provider ranks.
        Seen must be the full request exclusion set, not merely positive history.
        """
        history = _ids(history, 50)
        if isinstance(seen, (set, frozenset)) and len(seen) > 10_000:
            _fail("input_shape", "seen set exceeds the request limit")
        seen = frozenset(_ids(tuple(seen) if isinstance(seen, (set, frozenset)) else seen, 10_000))
        if not set(history).issubset(seen):
            _fail("input_shape", "seen set must include every history item")
        collaborative = _ids(collaborative, 200, unique=True)
        content = _ids(content, 200, unique=True)
        timestamp_ms = _timestamp(timestamp_ms)
        context, coherence = self._history(history)
        items = tuple(sorted(set(collaborative) | set(content)))
        cf_ranks = {item: 61 / (60 + rank) for rank, item in enumerate(collaborative, 1)}
        content_ranks = {item: 61 / (60 + rank) for rank, item in enumerate(content, 1)}
        candidates, scalars = [], []
        for item in items:
            vector = self.vector(item)
            metadata = self._metadata[self._indices[item]]
            if item in seen or metadata.first_seen_ms >= timestamp_ms:
                _fail("illegal_candidate", "candidate is seen or not strictly available")
            candidates.append(vector)
            scalars.append(tuple(_f32(value) for value in (
                sum(a * b for a, b in zip(context, vector, strict=True)),
                cf_ranks.get(item, 0.), content_ranks.get(item, 0.), metadata.prior_strength,
                float(not metadata.training_item),
                min(1., math.log1p((timestamp_ms - metadata.first_seen_ms) / 86400000) / math.log1p(3650)),
                math.log1p(len(history)) / math.log1p(50), coherence,
            )))
        binding = tuple((key, self.provenance[key]) for key in RANKER_BINDING_KEYS)
        return PoolFeatures(items, context, tuple(candidates), tuple(scalars), self.feature_fingerprint, binding)


def _feature_vector(value, length):
    if (not isinstance(value, list) or len(value) != length
            or any(type(v) not in (int, float) or abs(v) > 1.0001 or not math.isfinite(v) for v in value)):
        _fail("input_shape", "invalid finite reference feature vector")
    return value


def load_r06_features(component_dir, *, expected_manifest_sha256=None, expected_feature_fingerprint=None):
    """Controlled load with mandatory archived-feature replay. No activation."""
    supplied = Path(component_dir)
    if supplied.is_symlink():
        _fail("unsafe_path", "feature directory cannot be a symlink")
    root = supplied.resolve()
    try:
        names = set()
        for path in root.iterdir():
            names.add(path.name)
            if len(names) > 4:
                break
    except OSError as error:
        raise ControlledLoadError("component_unreadable", "cannot read feature directory") from error
    if names != {"manifest.json", "items.json", "vectors.f32", "validation.json"}:
        _fail("unsupported_format", "feature directory must contain exactly four controlled files")
    raw = _read(root, "manifest.json", MAX_JSON_BYTES)
    digest = hashlib.sha256(raw).hexdigest()
    if expected_manifest_sha256 is not None and digest != _digest(expected_manifest_sha256):
        _fail("component_changed", "feature manifest differs from the approved digest")
    manifest = _object(_json(raw), (
        "schema_version", "kind", "dtype", "dimension", "item_count", "history_decay",
        "history_limit", "rrf_constant", "scalar_names", "provenance", "items", "vectors", "validation",
    ))
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["kind"] != "r06-pool-features-v1" or manifest["dtype"] != "float32-le"):
        _fail("unsupported_format", "unsupported frozen feature format")
    if (type(manifest["history_decay"]) is not float or manifest["history_decay"] != .8
            or type(manifest["history_limit"]) is not int or manifest["history_limit"] != 50
            or type(manifest["rrf_constant"]) is not int or manifest["rrf_constant"] != 60
            or manifest["scalar_names"] != list(SCALAR_NAMES)):
        _fail("component_schema", "R06 feature protocol differs")
    dimension, count = _integer(manifest["dimension"], 128), _integer(manifest["item_count"], MAX_ITEMS)
    provenance = _object(manifest["provenance"], (*PROVENANCE_HASHES, "selection_protocol_id", "training_protocol_id"))
    for key in PROVENANCE_HASHES:
        _digest(provenance[key])
    for key in ("selection_protocol_id", "training_protocol_id"):
        _digest(provenance[key], 16)
    fingerprint = provenance["feature_fingerprint"]
    if expected_feature_fingerprint is not None and fingerprint != _digest(expected_feature_fingerprint):
        _fail("component_changed", "feature snapshot differs from the selected ranker")
    records = _json(_verified(root, manifest["items"], "items.json", MAX_JSON_BYTES))
    if not isinstance(records, list) or len(records) != count:
        _fail("input_shape", "item mapping count differs")
    ids, metadata = [], []
    for record in records:
        _object(record, ("item_id", "first_seen_ms", "training_item", "prior_strength"))
        item = _ids([record["item_id"]], 1)[0]
        prior = record["prior_strength"]
        if (type(record["training_item"]) is not bool or type(prior) not in (int, float)
                or not 0 <= prior <= 1 or not math.isfinite(prior)
                or (record["training_item"] is False and prior != 0)):
            _fail("component_schema", "invalid training flag or normalized prior")
        ids.append(item)
        metadata.append(ItemFeatures(_timestamp(record["first_seen_ms"]), record["training_item"], float(prior)))
    if ids != sorted(set(ids)):
        _fail("input_shape", "item mapping must be unique and sorted")
    _object(manifest["vectors"], ("path", "size_bytes", "sha256"))
    if manifest["vectors"]["size_bytes"] != 4 * count * dimension:
        _fail("input_shape", "vector byte count differs from the declared shape")
    vectors = _verified(root, manifest["vectors"], "vectors.f32", MAX_VECTOR_BYTES)
    present = []
    for index in range(count):
        vector = struct.unpack_from(f"<{dimension}f", vectors, index * dimension * 4)
        if any(not math.isfinite(value) or abs(value) > 1.0001 for value in vector):
            _fail("non_finite", "item vector must be finite and bounded")
        norm = math.sqrt(sum(value * value for value in vector))
        if norm >= 10 * 2**-23 and abs(norm - 1) > 1e-4:
            _fail("input_shape", "item vector must be normalized or an unscaled tiny vector")
        present.append(norm > 1e-8)
    measured = hashlib.sha256(vectors)
    measured.update(json.dumps(tuple(ids)).encode())
    if measured.hexdigest() != fingerprint:
        _fail("component_changed", "content feature fingerprint differs")
    samples = _json(_verified(root, manifest["validation"], "validation.json", MAX_JSON_BYTES))
    if not isinstance(samples, list) or not 1 <= len(samples) <= 4:
        _fail("resource_limit", "one to four reference requests are required")
    runtime = R06Features(dimension, tuple(ids), digest, fingerprint, len(samples),
                          MappingProxyType(dict(provenance)),
                          MappingProxyType({item: i for i, item in enumerate(ids)}),
                          tuple(metadata), bytes(present), vectors)
    for sample in samples:
        _object(sample, ("history", "seen", "timestamp_ms", "collaborative", "content",
                         "expected_items", "expected_context", "expected_scalars"))
        pool = runtime.build_pool(*(sample[key] for key in ("history", "seen", "timestamp_ms", "collaborative", "content")))
        if sample["expected_items"] != list(pool.item_ids):
            _fail("validation_mismatch", "candidate union differs from the archived pool")
        context = _feature_vector(sample["expected_context"], dimension)
        if any(abs(a - b) > FEATURE_TOLERANCE for a, b in zip(context, pool.context, strict=True)):
            _fail("validation_mismatch", "history context differs from the archived pool")
        expected = sample["expected_scalars"]
        if not isinstance(expected, list) or len(expected) != len(pool.scalars):
            _fail("input_shape", "reference scalar count differs")
        for reference, actual in zip(expected, pool.scalars, strict=True):
            reference = _feature_vector(reference, len(SCALAR_NAMES))
            if any(abs(a - b) > FEATURE_TOLERANCE for a, b in zip(reference, actual, strict=True)):
                _fail("validation_mismatch", "scalar features differ from the archived pool")
    return runtime
