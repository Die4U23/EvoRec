"""Controlled CPU loading for a small, non-executable bundle runtime format."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import struct
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from evorec.infrastructure.bundle import (
    BundleFile,
    BundleValidationError,
    ValidatedBundle,
    validate_bundle,
)


class ControlledLoadError(ValueError):
    """A safe bundle load failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LoadLimits:
    max_total_bytes: int = 512 * 1024 * 1024
    max_json_bytes: int = 32 * 1024 * 1024
    max_items: int = 500_000
    max_dimension: int = 4096
    max_embedding_values: int = 100_000_000
    max_validation_samples: int = 64
    score_tolerance: float = 1e-6
    normalization_tolerance: float = 1e-4

    def __post_init__(self):
        integer_fields = (
            self.max_total_bytes, self.max_json_bytes, self.max_items,
            self.max_dimension, self.max_embedding_values, self.max_validation_samples,
        )
        if any(type(value) is not int or value < 1 for value in integer_fields):
            raise ValueError("load limits must be positive integers")
        if (not math.isfinite(self.score_tolerance) or self.score_tolerance <= 0
                or not math.isfinite(self.normalization_tolerance)
                or self.normalization_tolerance <= 0):
            raise ValueError("load tolerances must be positive and finite")


@dataclass(frozen=True)
class RuntimeBundle:
    bundle_id: str
    manifest_sha256: str
    item_ids: tuple[str, ...]
    dimension: int
    model_id: str
    content_encoder_id: str
    semantic_codebook_id: str
    semantic_codes: tuple[str, ...]
    ranking_budget: int
    validation_samples_checked: int
    _item_indices: Mapping[str, int]
    _embedding_bytes: bytes
    _scale: float
    _biases: tuple[float, ...]

    def score(self, history_item_ids: Sequence[str],
              candidate_item_ids: Sequence[str]) -> tuple[float, ...]:
        """Score a bounded candidate list with the loaded safe baseline model."""
        if isinstance(history_item_ids, (str, bytes)) or isinstance(candidate_item_ids, (str, bytes)):
            raise ValueError("history and candidates must be item ID sequences")
        if any(not isinstance(item_id, str)
               for item_id in (*history_item_ids, *candidate_item_ids)):
            raise ValueError("history and candidate item IDs must be strings")
        if not candidate_item_ids or len(set(candidate_item_ids)) != len(candidate_item_ids):
            raise ValueError("candidate item IDs must be non-empty and unique")
        if len(candidate_item_ids) > self.ranking_budget:
            raise ValueError("candidate count exceeds the bundle ranking budget")
        try:
            history = [self._item_indices[item_id] for item_id in history_item_ids]
            candidates = [self._item_indices[item_id] for item_id in candidate_item_ids]
        except KeyError as error:
            raise ValueError(f"unknown item ID: {error.args[0]}") from error

        query = [0.0] * self.dimension
        if history:
            for internal_id in history:
                vector = self._vector(internal_id)
                for index, value in enumerate(vector):
                    query[index] += value
            norm = math.sqrt(sum(value * value for value in query))
            if norm > 0:
                query = [value / norm for value in query]
        return tuple(
            self._scale * sum(left * right for left, right in zip(query, self._vector(item), strict=True))
            + self._biases[item]
            for item in candidates
        )

    def _vector(self, internal_id: int) -> tuple[float, ...]:
        return struct.unpack_from(
            f"<{self.dimension}f", self._embedding_bytes,
            internal_id * self.dimension * 4,
        )


def _fail(code: str, message: str) -> None:
    raise ControlledLoadError(code, message)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("invalid_json", f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    _fail("invalid_json", f"non-standard JSON constant is not allowed: {value}")


def _json_bytes(raw: bytes, name: str, limits: LoadLimits) -> Any:
    if len(raw) > limits.max_json_bytes:
        _fail("resource_limit", f"{name} exceeds the JSON byte limit")
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail("invalid_json", f"{name} is not valid UTF-8 JSON: {error}")


def _object(value: Any, name: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        actual = set(value) if isinstance(value, dict) else set()
        _fail("component_schema", f"{name} keys mismatch; "
              f"missing={sorted(keys - actual)}, extra={sorted(actual - keys)}")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("component_schema", f"{name} must be a non-empty string")
    return value


def _integer(value: Any, name: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        _fail("component_schema", f"{name} must be an integer >= {minimum}")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("component_schema", f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        _fail("non_finite", f"{name} must be finite")
    return result


def _schema_one(value: Any, name: str) -> None:
    if type(value) is not int or value != 1:
        _fail("component_schema", f"{name}.schema_version must be 1")


def _role_file(manifest: dict[str, Any], files: Mapping[str, BundleFile],
               field: str, suffix: str) -> BundleFile:
    role = manifest["artifacts"][field]
    entry = files.get(role)
    if entry is None:
        _fail("component_missing", f"artifact role is not listed: {role}")
    if not entry.path.lower().endswith(suffix):
        _fail("unsupported_format", f"{field} must use {suffix}: {entry.path}")
    return entry


def _verified_bytes(bundle: ValidatedBundle, entry: BundleFile) -> bytes:
    path = bundle.root.joinpath(*PurePosixPath(entry.path).parts)
    if path.is_symlink() or not path.is_file():
        _fail("bundle_changed", f"artifact is missing or no longer regular: {entry.path}")
    try:
        raw = path.read_bytes()
    except OSError as error:
        _fail("component_unreadable", f"cannot read {entry.path}: {error}")
    if len(raw) != entry.size_bytes or hashlib.sha256(raw).hexdigest() != entry.sha256:
        _fail("bundle_changed", f"artifact changed after validation: {entry.path}")
    return raw


def _check_bundle_limits(bundle: ValidatedBundle, limits: LoadLimits) -> None:
    if bundle.item_count > limits.max_items or bundle.embedding_dimension > limits.max_dimension:
        _fail("resource_limit", "bundle exceeds item or embedding dimension limit")
    if bundle.item_count * bundle.embedding_dimension > limits.max_embedding_values:
        _fail("resource_limit", "bundle exceeds embedding value limit")
    if sum(entry.size_bytes for entry in bundle.files) > limits.max_total_bytes:
        _fail("resource_limit", "bundle exceeds total artifact byte limit")


def _fresh_bundle(bundle: ValidatedBundle, manifest: dict[str, Any],
                  limits: LoadLimits) -> ValidatedBundle:
    files = {entry.role: entry for entry in bundle.files}
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        _fail("component_schema", "manifest.artifacts must be an object")
    json_fields = (
        "item_mapping_role", "ranker_role", "content_encoder_role",
        "semantic_codebook_role", "vector_index_role",
    )
    for field in json_fields:
        role = artifacts.get(field)
        entry = files.get(role)
        if entry is None or entry.size_bytes > limits.max_json_bytes:
            _fail("resource_limit", f"{field} is missing or exceeds the JSON byte limit")
    try:
        refreshed = validate_bundle(bundle.root.parent, bundle.root)
    except BundleValidationError as error:
        raise ControlledLoadError("bundle_changed", str(error)) from error
    if refreshed != bundle:
        _fail("bundle_changed", "bundle identity changed after validation")
    return refreshed


def _load_manifest(bundle: ValidatedBundle, limits: LoadLimits) -> dict[str, Any]:
    path = bundle.root / "manifest.json"
    try:
        if path.stat().st_size > limits.max_json_bytes:
            _fail("resource_limit", "manifest.json exceeds the JSON byte limit")
        raw = path.read_bytes()
    except OSError as error:
        _fail("bundle_changed", f"cannot reread manifest: {error}")
    if hashlib.sha256(raw).hexdigest() != bundle.manifest_sha256:
        _fail("bundle_changed", "manifest changed after validation")
    manifest = _json_bytes(raw, "manifest.json", limits)
    if not isinstance(manifest, dict):
        _fail("component_schema", "manifest must be an object")
    return manifest


def _load_mapping(raw: bytes, expected_count: int, limits: LoadLimits) -> tuple[str, ...]:
    document = _json_bytes(raw, "item mapping", limits)
    if not isinstance(document, list) or len(document) != expected_count:
        _fail("component_schema", "item mapping length mismatch")
    items = []
    for expected_id, value in enumerate(document):
        row = _object(value, f"item_mapping[{expected_id}]", {"internal_id", "item_id"})
        if type(row["internal_id"]) is not int or row["internal_id"] != expected_id:
            _fail("component_schema", "item mapping IDs must be contiguous from zero")
        items.append(_string(row["item_id"], f"item_mapping[{expected_id}].item_id"))
    return tuple(items)


def _validate_embeddings(raw: bytes, item_count: int, dimension: int,
                         tolerance: float) -> None:
    if len(raw) != item_count * dimension * 4:
        _fail("component_mismatch", "float32 embedding byte count mismatch")
    for item in range(item_count):
        vector = struct.unpack_from(f"<{dimension}f", raw, item * dimension * 4)
        if not all(math.isfinite(value) for value in vector):
            _fail("non_finite", f"embedding {item} contains a non-finite value")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isclose(norm, 1.0, rel_tol=tolerance, abs_tol=tolerance):
            _fail("component_mismatch", f"embedding {item} is not normalized")


def _load_encoder(raw: bytes, manifest: dict[str, Any], dimension: int,
                  limits: LoadLimits) -> str:
    value = _object(_json_bytes(raw, "content encoder", limits), "content_encoder", {
        "schema_version", "kind", "model_id", "dimension", "dtype", "output_normalized",
    })
    _schema_one(value["schema_version"], "content_encoder")
    if (value["kind"] != "mean-history-v1"
            or value["dtype"] != "float32" or value["output_normalized"] is not True
            or _integer(value["dimension"], "content_encoder.dimension") != dimension):
        _fail("component_mismatch", "unsupported or incompatible content encoder contract")
    model_id = _string(value["model_id"], "content_encoder.model_id")
    if model_id != manifest["artifacts"]["content_encoder_id"]:
        _fail("component_mismatch", "content encoder ID mismatch")
    return model_id


def _load_codebook(raw: bytes, manifest: dict[str, Any], item_count: int,
                   limits: LoadLimits) -> tuple[str, tuple[str, ...]]:
    value = _object(_json_bytes(raw, "semantic codebook", limits), "semantic_codebook", {
        "schema_version", "kind", "codebook_id", "item_count", "codes",
    })
    _schema_one(value["schema_version"], "semantic_codebook")
    if (value["kind"] != "item-codebook-v1"
            or _integer(value["item_count"], "semantic_codebook.item_count") != item_count):
        _fail("component_mismatch", "unsupported or incompatible semantic codebook")
    codebook_id = _string(value["codebook_id"], "semantic_codebook.codebook_id")
    if codebook_id != manifest["artifacts"]["semantic_codebook_id"]:
        _fail("component_mismatch", "semantic codebook ID mismatch")
    codes = value["codes"]
    if (not isinstance(codes, list) or len(codes) != item_count
            or any(not isinstance(code, str) or not code for code in codes)
            or len(set(codes)) != len(codes)):
        _fail("component_mismatch", "semantic codes must be unique non-empty strings")
    return codebook_id, tuple(codes)


def _load_index(raw: bytes, manifest: dict[str, Any], item_count: int,
                dimension: int, limits: LoadLimits) -> None:
    value = _object(_json_bytes(raw, "vector index", limits), "vector_index", {
        "schema_version", "kind", "item_count", "dimension", "dtype",
        "normalized", "distance", "internal_ids",
    })
    _schema_one(value["schema_version"], "vector_index")
    expected_ids = list(range(item_count))
    if (value["kind"] != "flat-v1"
            or _integer(value["item_count"], "vector_index.item_count") != item_count
            or _integer(value["dimension"], "vector_index.dimension") != dimension
            or value["dtype"] != "float32" or value["normalized"] is not True
            or value["distance"] != "cosine" or value["internal_ids"] != expected_ids
            or manifest["index"]["kind"] != "flat"
            or manifest["index"]["distance"] != "cosine"
            or manifest["index"]["normalized"] is not True):
        _fail("component_mismatch", "unsupported or incompatible vector index")


def _load_ranker(raw: bytes, manifest: dict[str, Any], item_count: int,
                 dimension: int, limits: LoadLimits) -> tuple[str, float, tuple[float, ...], list[Any]]:
    value = _object(_json_bytes(raw, "ranker", limits), "ranker", {
        "schema_version", "kind", "model_id", "dimension", "scale", "biases",
        "validation_samples",
    })
    _schema_one(value["schema_version"], "ranker")
    if (value["kind"] != "dot-product-v1"
            or _integer(value["dimension"], "ranker.dimension") != dimension):
        _fail("component_mismatch", "unsupported or incompatible ranker")
    model_id = _string(value["model_id"], "ranker.model_id")
    if model_id != manifest["artifacts"]["ranker_id"]:
        _fail("component_mismatch", "ranker ID mismatch")
    scale = _number(value["scale"], "ranker.scale")
    biases = value["biases"]
    if not isinstance(biases, list) or len(biases) != item_count:
        _fail("component_mismatch", "ranker biases must match item count")
    parsed_biases = tuple(_number(item, f"ranker.biases[{index}]")
                          for index, item in enumerate(biases))
    samples = value["validation_samples"]
    if (not isinstance(samples, list) or not samples
            or len(samples) > limits.max_validation_samples):
        _fail("resource_limit", "ranker validation sample count is invalid")
    return model_id, scale, parsed_biases, samples


def _check_samples(runtime: RuntimeBundle, samples: list[Any], limits: LoadLimits) -> None:
    for index, value in enumerate(samples):
        sample = _object(value, f"validation_samples[{index}]", {
            "history_item_ids", "candidate_item_ids", "expected_scores", "expected_order",
        })
        history = sample["history_item_ids"]
        candidates = sample["candidate_item_ids"]
        expected_scores = sample["expected_scores"]
        expected_order = sample["expected_order"]
        if (not isinstance(history, list) or not isinstance(candidates, list)
                or not isinstance(expected_scores, list) or not isinstance(expected_order, list)
                or any(not isinstance(item, str) for item in history + candidates + expected_order)
                or len(candidates) != len(expected_scores) or len(candidates) != len(expected_order)):
            _fail("sample_mismatch", f"validation sample {index} has invalid sequences")
        try:
            actual_scores = runtime.score(history, candidates)
        except ValueError as error:
            _fail("sample_mismatch", f"validation sample {index} cannot run: {error}")
        parsed_expected = tuple(
            _number(score, f"validation_samples[{index}].expected_scores")
            for score in expected_scores
        )
        if any(not math.isclose(actual, expected, rel_tol=limits.score_tolerance,
                                abs_tol=limits.score_tolerance)
               for actual, expected in zip(actual_scores, parsed_expected, strict=True)):
            _fail("sample_mismatch", f"validation sample {index} score mismatch")
        actual_order = tuple(item for _, item in sorted(
            zip(actual_scores, candidates, strict=True), key=lambda pair: -pair[0]))
        if actual_order != tuple(expected_order):
            _fail("sample_mismatch", f"validation sample {index} order mismatch")


def load_runtime_bundle(bundle: ValidatedBundle,
                        limits: LoadLimits | None = None) -> RuntimeBundle:
    """Revalidate and load a safe CPU runtime, then replay its golden samples."""
    limits = limits or LoadLimits()
    _check_bundle_limits(bundle, limits)
    manifest = _load_manifest(bundle, limits)
    bundle = _fresh_bundle(bundle, manifest, limits)
    files = {entry.role: entry for entry in bundle.files}

    mapping_entry = _role_file(manifest, files, "item_mapping_role", ".json")
    embeddings_entry = _role_file(manifest, files, "item_embeddings_role", ".f32")
    ranker_entry = _role_file(manifest, files, "ranker_role", ".json")
    encoder_entry = _role_file(manifest, files, "content_encoder_role", ".json")
    codebook_entry = _role_file(manifest, files, "semantic_codebook_role", ".json")
    index_entry = _role_file(manifest, files, "vector_index_role", ".json")
    if bundle.embedding_dtype != "float32":
        _fail("unsupported_format", "controlled runtime currently accepts float32 embeddings only")

    item_ids = _load_mapping(_verified_bytes(bundle, mapping_entry), bundle.item_count, limits)
    embedding_bytes = _verified_bytes(bundle, embeddings_entry)
    _validate_embeddings(embedding_bytes, bundle.item_count, bundle.embedding_dimension,
                         limits.normalization_tolerance)
    encoder_id = _load_encoder(_verified_bytes(bundle, encoder_entry), manifest,
                               bundle.embedding_dimension, limits)
    codebook_id, codes = _load_codebook(_verified_bytes(bundle, codebook_entry), manifest,
                                        bundle.item_count, limits)
    _load_index(_verified_bytes(bundle, index_entry), manifest, bundle.item_count,
                bundle.embedding_dimension, limits)
    model_id, scale, biases, samples = _load_ranker(
        _verified_bytes(bundle, ranker_entry), manifest, bundle.item_count,
        bundle.embedding_dimension, limits,
    )
    item_indices = MappingProxyType({item_id: index for index, item_id in enumerate(item_ids)})
    ranking_budget = manifest["strategy"]["ranking_budget"]
    runtime = RuntimeBundle(
        bundle.bundle_id, bundle.manifest_sha256, item_ids, bundle.embedding_dimension,
        model_id, encoder_id, codebook_id, codes, ranking_budget, len(samples), item_indices,
        embedding_bytes, scale, biases,
    )
    _check_samples(runtime, samples, limits)
    return runtime
