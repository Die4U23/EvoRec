"""Bounded, non-executable frozen TF-IDF/SVD transform; not an online bundle.

The service never loads pickle/joblib or imports research numerical libraries.
Hashes detect drift, not authorship; approve and pin a trusted manifest first.
"""

from dataclasses import dataclass
import hashlib
from itertools import chain
import math
from pathlib import Path
import re
import struct
from types import MappingProxyType
from typing import Mapping
import unicodedata

from evorec.infrastructure.r06_features import R06Features, _json, _timestamp
from evorec.infrastructure.residual_ranker import (
    ControlledLoadError, _digest, _f32, _integer, _object, _read, _verified,
)

MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_WEIGHT_BYTES = 16 * 1024 * 1024
MAX_TERMS = 20_000
MAX_TEXT_CHARS = 32_768
MAX_TOKENS = 4096
MAX_REFERENCE_CHARS = 131_072
VECTOR_TOLERANCE = 1e-6  # Fixed locally, never selected by the artifact.
TOKEN_PATTERN = r"(?u)\b\w\w+\b"
_WORD = re.compile(TOKEN_PATTERN)
BINDING_KEYS = (
    "feature_fingerprint", "selection_series_sha256", "source_series_sha256",
    "items_sha256", "vectors_sha256", "selection_protocol_id", "training_protocol_id",
)
PROVENANCE_HASHES = (
    "feature_fingerprint", "selection_series_sha256", "source_series_sha256",
    "encoder_sha256", "items_sha256", "vectors_sha256", "metadata_sha256",
    "fit_item_set_sha256", "fit_training_signature", "training_sample_sha256",
)


def _fail(code, message):
    raise ControlledLoadError(code, message)


def text_protocol():
    return {"lowercase": True, "token_pattern": TOKEN_PATTERN, "ngram_range": [1, 2],
            "sublinear_tf": True, "use_idf": True, "smooth_idf": True, "norm": "l2",
            "unicode_version": unicodedata.unidata_version}


def _tokens(text):
    if not isinstance(text, str):
        _fail("input_shape", "text must be a Unicode string")
    if len(text) > MAX_TEXT_CHARS:
        _fail("resource_limit", "text exceeds the character limit")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ControlledLoadError("input_shape", "text must be valid UTF-8") from error
    result = []
    for match in _WORD.finditer(text.lower()):
        if len(result) == MAX_TOKENS:
            _fail("resource_limit", "text exceeds the token limit")
        result.append(match.group())
    return result


def _unit_vector(values, dimension):
    if not isinstance(values, list) or len(values) != dimension:
        _fail("input_shape", "reference vector dimension differs")
    if any(type(v) not in (int, float) or abs(v) > 1.0001 or not math.isfinite(v) for v in values):
        _fail("non_finite", "reference vector must be finite and bounded")
    norm = math.sqrt(math.fsum(v * v for v in values))
    if norm >= 10 * 2**-23 and abs(norm - 1) > 1e-4:
        _fail("input_shape", "reference vector must be normalized or unscaled tiny")
    return tuple(values)


@dataclass(frozen=True)
class ContentEncoder:
    dimension: int
    vocabulary_terms: int
    manifest_sha256: str
    validation_samples_checked: int
    provenance: Mapping[str, str]
    _vocabulary: Mapping[str, int]
    _idf: tuple[float, ...]
    _components: bytes  # Feature-major float32 rows, each with dimension columns.

    def encode(self, text: str) -> tuple[float, ...]:
        tokens = _tokens(text)
        counts = {}
        for term in chain(tokens, (a + " " + b for a, b in zip(tokens, tokens[1:]))):
            index = self._vocabulary.get(term)
            if index is not None:
                counts[index] = counts.get(index, 0) + 1
        if not counts:
            return (0.,) * self.dimension
        # Preserve sorted CSR index order and float32 TF/IDF/projection arithmetic.
        values = [(i, _f32(_f32(_f32(math.log(count)) + 1.) * self._idf[i]))
                  for i, count in sorted(counts.items())]
        norm = math.sqrt(math.fsum(value * value for _, value in values))
        result = [0.] * self.dimension
        for index, value in values:
            value = _f32(value / norm)
            row = struct.unpack_from(f"<{self.dimension}f", self._components, index * self.dimension * 4)
            for column, coefficient in enumerate(row):
                result[column] = _f32(result[column] + _f32(value * coefficient))
        norm = _f32(math.sqrt(math.fsum(value * value for value in result)))
        # sklearn normalize does not amplify float32 vectors below 10 eps.
        if norm < 10 * 2**-23:
            return tuple(result)
        return tuple(_f32(value / norm) for value in result)

    def check_features(self, features: R06Features):
        if not isinstance(features, R06Features):
            _fail("input_shape", "a controlled R06 feature component is required")
        if (features.dimension != self.dimension
                or any(features.provenance.get(key) != self.provenance[key] for key in BINDING_KEYS)):
            _fail("component_changed", "encoder and frozen feature snapshot do not match")


def load_content_encoder(component_dir: Path, *, expected_manifest_sha256=None,
                         expected_feature_fingerprint=None) -> ContentEncoder:
    supplied = Path(component_dir)
    if supplied.is_symlink():
        _fail("unsafe_path", "encoder directory cannot be a symlink")
    root = supplied.resolve()
    try:
        names = set()
        for path in root.iterdir():
            names.add(path.name)
            if len(names) > 4:
                break
    except OSError as error:
        raise ControlledLoadError("component_unreadable", "cannot read encoder directory") from error
    if names != {"manifest.json", "vocabulary.json", "weights.f32", "validation.json"}:
        _fail("unsupported_format", "encoder requires exactly four controlled files")
    raw = _read(root, "manifest.json", MAX_JSON_BYTES)
    digest = hashlib.sha256(raw).hexdigest()
    if expected_manifest_sha256 is not None and digest != _digest(expected_manifest_sha256):
        _fail("component_changed", "encoder manifest differs from the approved digest")
    manifest = _object(_json(raw), (
        "schema_version", "kind", "dtype", "dimension", "vocabulary_terms", "text_protocol",
        "provenance", "fit", "vocabulary", "weights", "validation",
    ))
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["kind"] != "frozen-tfidf-svd-v1" or manifest["dtype"] != "float32-le"):
        _fail("unsupported_format", "unsupported text encoder format")
    protocol = _object(manifest["text_protocol"], text_protocol())
    if (any(type(protocol[k]) is not type(v) or protocol[k] != v for k, v in text_protocol().items())
            or any(type(n) is not int for n in protocol["ngram_range"])):
        _fail("component_schema", "text analysis differs from the frozen protocol")
    dimension = _integer(manifest["dimension"], 128)
    count = _integer(manifest["vocabulary_terms"], MAX_TERMS)
    if dimension > count:
        _fail("input_shape", "SVD dimension exceeds vocabulary size")
    provenance = _object(manifest["provenance"], (*PROVENANCE_HASHES, "selected_method",
                                                "selection_protocol_id", "training_protocol_id", "encoder_protocol_id"))
    if provenance["selected_method"] != "A-frozen-s17":
        _fail("component_schema", "only the validation-selected frozen arm is supported")
    for key in PROVENANCE_HASHES:
        _digest(provenance[key])
    for key in ("selection_protocol_id", "training_protocol_id", "encoder_protocol_id"):
        _digest(provenance[key], 16)
    if (expected_feature_fingerprint is not None
            and provenance["feature_fingerprint"] != _digest(expected_feature_fingerprint)):
        _fail("component_changed", "encoder feature fingerprint differs")
    fit = _object(manifest["fit"], ("end_ms", "training_rows", "document_count"))
    _timestamp(fit["end_ms"])
    _integer(fit["training_rows"], 10_000_000)
    _integer(fit["document_count"], 200_000)
    if fit["document_count"] > fit["training_rows"]:
        _fail("component_schema", "fit document count exceeds training rows")
    terms = _json(_verified(root, manifest["vocabulary"], "vocabulary.json", MAX_JSON_BYTES))
    if not isinstance(terms, list) or len(terms) != count:
        _fail("input_shape", "vocabulary count differs")
    for term in terms:
        if (not isinstance(term, str) or len(term) > 256 or term != term.lower()
                or not 1 <= len(term.split(" ")) <= 2
                or any(_WORD.fullmatch(token) is None for token in term.split(" "))):
            _fail("component_schema", "vocabulary must contain canonical word unigrams/bigrams")
    if terms != sorted(set(terms)):
        _fail("input_shape", "vocabulary must be unique and in original sorted index order")
    _object(manifest["weights"], ("path", "size_bytes", "sha256"))
    if manifest["weights"]["size_bytes"] != 4 * count * (dimension + 1):
        _fail("input_shape", "encoder weight shape differs")
    weights = _verified(root, manifest["weights"], "weights.f32", MAX_WEIGHT_BYTES)
    idf = struct.unpack_from(f"<{count}f", weights)
    if any(not math.isfinite(v) or not 1 <= v <= 32 for v in idf):
        _fail("non_finite", "IDF must be finite and bounded")
    components = weights[count * 4:]
    if any(not math.isfinite(v[0]) or abs(v[0]) > 1.0001 for v in struct.iter_unpack("<f", components)):
        _fail("non_finite", "SVD coefficients must be finite and bounded")
    samples = _json(_verified(root, manifest["validation"], "validation.json", MAX_JSON_BYTES))
    if not isinstance(samples, list) or not 2 <= len(samples) <= 16:
        _fail("resource_limit", "two to sixteen text reference samples are required")
    runtime = ContentEncoder(dimension, count, digest, len(samples), MappingProxyType(dict(provenance)),
                             MappingProxyType({term: i for i, term in enumerate(terms)}), idf, components)
    total, empty, signal = 0, False, False
    for sample in samples:
        _object(sample, ("text", "expected_vector"))
        if not isinstance(sample["text"], str):
            _fail("input_shape", "reference text must be a string")
        total += len(sample["text"])
        if total > MAX_REFERENCE_CHARS:
            _fail("resource_limit", "reference text total exceeds limit")
        actual = runtime.encode(sample["text"])
        expected = _unit_vector(sample["expected_vector"], dimension)
        if any(abs(a - b) > VECTOR_TOLERANCE for a, b in zip(actual, expected, strict=True)):
            _fail("validation_mismatch", "text vector differs from the frozen encoder")
        empty |= sample["text"] == "" and all(v == 0 for v in actual)
        signal |= any(abs(v) > 1e-8 for v in actual)
    if not empty or not signal:
        _fail("validation_mismatch", "empty-text and represented-text references are required")
    return runtime
