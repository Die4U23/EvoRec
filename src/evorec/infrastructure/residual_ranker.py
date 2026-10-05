"""Non-executable R06 ranker component; not an online recommendation bundle.

Only JSON and bounded little-endian float32 arrays are read. Feature construction,
candidate retrieval and publication intentionally remain outside this component.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import math
from pathlib import Path
import re
import struct
from types import MappingProxyType
from typing import Mapping, Sequence

from evorec.infrastructure.model_runtime import ControlledLoadError

SCALAR_NAMES = (
    "content_cosine", "cf_rrf", "content_rrf", "prior_strength",
    "model_cold", "log_age", "history_length", "history_coherence",
)
PROVENANCE_HASHES = (
    "selection_series_sha256", "source_series_sha256", "replication_series_sha256",
    "checkpoint_sha256", "feature_fingerprint", "validation_pool_sha256",
    "items_sha256", "vectors_sha256",
)
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_WEIGHT_BYTES = 4 * 1024 * 1024
MAX_CANDIDATES = 400
SCORE_TOLERANCE = 1e-5  # Absolute, fixed by the loader, not supplied by the artifact.


def _fail(code, message):
    raise ControlledLoadError(code, message)


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        _fail("component_schema", "unexpected component fields")
    return value


def _number(value):
    if (type(value) not in (int, float) or abs(value) > 1_000_000
            or not math.isfinite(value)):
        _fail("non_finite", "numeric value must be finite and bounded")
    return float(value)


def _integer(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        _fail("resource_limit", "dimension or count exceeds supported limits")
    return value


def _digest(value, length=64):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % length, value):
        _fail("component_schema", "invalid provenance digest")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("invalid_json", "duplicate JSON field")
        result[key] = value
    return result


def _constant(_value):
    _fail("invalid_json", "non-finite JSON constant")


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ControlledLoadError("invalid_json", "invalid UTF-8 JSON") from error


def _read(root, name, limit):
    path = root / name
    try:
        if path.is_symlink() or path.resolve().parent != root or not path.is_file():
            _fail("unsafe_path", "component file must be a regular local file")
        if path.stat().st_size > limit:
            _fail("resource_limit", "component file exceeds byte limit")
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            _fail("resource_limit", "component file grew beyond byte limit")
        return raw
    except OSError as error:
        raise ControlledLoadError("component_unreadable", "cannot read component file") from error


def _verified(root, record, name, limit):
    _object(record, ("path", "size_bytes", "sha256"))
    if record["path"] != name:
        _fail("unsafe_path", "only fixed component filenames are supported")
    size = _integer(record["size_bytes"], limit)
    digest = _digest(record["sha256"])
    raw = _read(root, name, limit)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        _fail("component_changed", "component size or hash mismatch")
    return raw


def _vector(values, length):
    if (not isinstance(values, Sequence) or isinstance(values, (str, bytes))
            or len(values) != length):
        _fail("input_shape", "vector length mismatch")
    return tuple(_number(value) for value in values)


def _linear(values, layer):
    weights, biases = layer
    return tuple(bias + sum(a * b for a, b in zip(row, values, strict=True))
                 for row, bias in zip(weights, biases, strict=True))


def _gelu(value):
    return .5 * value * (1 + math.erf(value / math.sqrt(2)))


def _f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


@dataclass(frozen=True)
class ResidualRanker:
    dimension: int
    hidden: int
    bottleneck: int
    base_scale: float
    residual_scale: float
    manifest_sha256: str
    provenance: Mapping[str, str]
    validation_samples_checked: int
    _layers: tuple
    _accelerated: object = field(default=None, repr=False, compare=False)

    def score(self, context: Sequence[float], candidates: Sequence[Sequence[float]],
              scalars: Sequence[Sequence[float]]) -> tuple[float, ...]:
        """Score already-legal, unpadded candidates in their original stable order.

        Caller must build the original CF/content union and exact R06 features.
        This does not select candidates, read IDs or infer scalars from metadata.
        """
        context = _vector(context, self.dimension)
        if (not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes))
                or not 1 <= len(candidates) <= MAX_CANDIDATES
                or not isinstance(scalars, Sequence) or isinstance(scalars, (str, bytes))
                or len(scalars) != len(candidates)):
            _fail("input_shape", "candidate count or scalar shape mismatch")
        empty = math.sqrt(sum(value * value for value in context)) <= 1e-8
        if self._accelerated is not None:
            rows = tuple(_vector(candidate, self.dimension) for candidate in candidates)
            fields = tuple(_vector(scalar, len(SCALAR_NAMES)) for scalar in scalars)
            return self._accelerated.score(context, rows, fields, empty=empty)
        result = []
        for candidate, scalar in zip(candidates, scalars, strict=True):
            candidate = _vector(candidate, self.dimension)
            scalar = _vector(scalar, len(SCALAR_NAMES))
            base = self.base_scale * .5 * (scalar[1] + scalar[2])
            if empty:
                result.append(_f32(base))
                continue
            values = (*context, *candidate,
                      *(a * b for a, b in zip(context, candidate, strict=True)),
                      *(abs(a - b) for a, b in zip(context, candidate, strict=True)), *scalar)
            values = tuple(_gelu(v) for v in _linear(values, self._layers[0]))
            values = tuple(_gelu(v) for v in _linear(values, self._layers[1]))
            delta = self.residual_scale * math.tanh(_linear(values, self._layers[2])[0])
            result.append(_f32(base + delta))
        return tuple(result)


def load_residual_ranker(component_dir: Path, *, expected_manifest_sha256: str | None = None,
                         cpu_backend: str = "stdlib"
                         ) -> ResidualRanker:
    """Validate hashes, shapes, finite weights and mandatory reference replay.

    Hashes detect drift, not authorship. Load only trusted immutable directories;
    pin expected_manifest_sha256 when selecting a previously approved component.
    No registration, database access or active-model mutation takes place.
    """
    if type(cpu_backend) is not str or cpu_backend not in {"stdlib", "numpy"}:
        _fail("unsupported_backend", "ranker backend must be explicitly stdlib or numpy")
    supplied = Path(component_dir)
    if supplied.is_symlink():
        _fail("unsafe_path", "component directory cannot be a symlink")
    root = supplied.resolve()
    try:
        names = set()
        for path in root.iterdir():
            names.add(path.name)
            if len(names) > 3:
                break
    except OSError as error:
        raise ControlledLoadError("component_unreadable", "cannot read component directory") from error
    if names != {"manifest.json", "weights.f32", "validation.json"}:
        _fail("unsupported_format", "directory must contain exactly three controlled files")
    raw = _read(root, "manifest.json", MAX_JSON_BYTES)
    digest = hashlib.sha256(raw).hexdigest()
    if expected_manifest_sha256 is not None and digest != _digest(expected_manifest_sha256):
        _fail("component_changed", "manifest differs from the approved digest")
    manifest = _object(_json(raw), (
        "schema_version", "kind", "dtype", "dimension", "hidden", "bottleneck",
        "base_scale", "residual_scale", "scalar_names", "provenance", "weights", "validation",
    ))
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["kind"] != "residual-list-mlp-v1" or manifest["dtype"] != "float32-le"):
        _fail("unsupported_format", "unsupported ranker architecture or dtype")
    if manifest["scalar_names"] != list(SCALAR_NAMES):
        _fail("component_schema", "scalar order differs from R06")
    dimension = _integer(manifest["dimension"], 128)
    hidden = _integer(manifest["hidden"], 256)
    bottleneck = _integer(manifest["bottleneck"], 128)
    base_scale, residual_scale = (_number(manifest[key]) for key in ("base_scale", "residual_scale"))
    if not 0 < base_scale <= 1000 or not 0 <= residual_scale <= 1000:
        _fail("component_schema", "invalid ranker scales")
    provenance = _object(manifest["provenance"], (
        *PROVENANCE_HASHES, "selected_method", "selection_protocol_id", "training_protocol_id",
    ))
    if provenance["selected_method"] != "A-frozen-s17":
        _fail("component_schema", "only the validation-selected frozen R06 arm is supported")
    for key in PROVENANCE_HASHES:
        _digest(provenance[key])
    for key in ("selection_protocol_id", "training_protocol_id"):
        _digest(provenance[key], 16)
    shapes = ((4 * dimension + len(SCALAR_NAMES), hidden), (hidden, bottleneck), (bottleneck, 1))
    expected_bytes = 4 * sum(inputs * outputs + outputs for inputs, outputs in shapes)
    _object(manifest["weights"], ("path", "size_bytes", "sha256"))
    if manifest["weights"]["size_bytes"] != expected_bytes:
        _fail("input_shape", "weight shape does not match the declared architecture")
    weights = _verified(root, manifest["weights"], "weights.f32", MAX_WEIGHT_BYTES)
    values = tuple(_number(value[0]) for value in struct.iter_unpack("<f", weights))
    layers, offset = [], 0
    for inputs, outputs in shapes:
        matrix = tuple(values[offset + row * inputs:offset + (row + 1) * inputs]
                       for row in range(outputs))
        offset += inputs * outputs
        biases = values[offset:offset + outputs]
        offset += outputs
        layers.append((matrix, biases))
    samples = _json(_verified(root, manifest["validation"], "validation.json", MAX_JSON_BYTES))
    if not isinstance(samples, list) or not 1 <= len(samples) <= 4:
        _fail("resource_limit", "one to four reference samples are required")
    runtime = ResidualRanker(dimension, hidden, bottleneck, base_scale, residual_scale,
                             digest, MappingProxyType(dict(provenance)), len(samples), tuple(layers))
    _validate_samples(runtime, samples)
    if cpu_backend == "numpy":
        from evorec.infrastructure._ranker_numpy import numpy_mlp

        runtime = replace(runtime, _accelerated=numpy_mlp(runtime))
        _validate_samples(runtime, samples)  # Both CPU and accelerated execution must pass.
    return runtime


def _validate_samples(runtime, samples):
    for sample in samples:
        _object(sample, ("context", "candidates", "scalars", "expected_scores", "expected_top20"))
        actual = runtime.score(sample["context"], sample["candidates"], sample["scalars"])
        expected = _vector(sample["expected_scores"], len(actual))
        if any(abs(a - b) > SCORE_TOLERANCE for a, b in zip(actual, expected, strict=True)):
            _fail("validation_mismatch", "reference scores differ from the frozen checkpoint")
        order = sorted(range(len(actual)), key=lambda index: -actual[index])[:20]
        reference_order = sample["expected_top20"]
        if (not isinstance(reference_order, list) or any(type(i) is not int for i in reference_order)
                or reference_order != order):
            _fail("validation_mismatch", "reference Top-20 order differs from the checkpoint")
