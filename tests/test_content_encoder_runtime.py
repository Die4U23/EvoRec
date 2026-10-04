"""Analytical synthetic references and hostile inputs, without research packages."""

from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
from types import MappingProxyType

import pytest

from evorec.infrastructure.content_encoder import (
    BINDING_KEYS, MAX_TEXT_CHARS, MAX_TOKENS, PROVENANCE_HASHES,
    load_content_encoder, text_protocol,
)
from evorec.infrastructure.r06_features import R06Features
from evorec.infrastructure.residual_ranker import ControlledLoadError

TERMS = ["123", "alpha", "alpha beta", "beta", "café", "under_score", "你好"]
IDF = [1., 1., 1.5, 2., 2.5, 2., 3.]
ROWS = [[.4, .1], [.5, .25], [.2, -.1], [-.25, .5], [.75, -.25], [.1, -.4], [.1, .4]]


def _reference(counts):
    # Independent ideal arithmetic, not a call to the runtime tokenizer or encoder.
    tfidf = {i: (1 + math.log(n)) * IDF[i] for i, n in counts.items()}
    norm = math.sqrt(sum(v*v for v in tfidf.values()))
    raw = [sum(value / norm * ROWS[i][column] for i, value in tfidf.items()) for column in range(2)] if norm else [0., 0.]
    length = math.sqrt(sum(v*v for v in raw))
    return [v / length for v in raw] if length else raw


def _record(name, raw):
    return {"path": name, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _manifest(root, manifest):
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _change(root, manifest, key, raw):
    name = manifest[key]["path"]
    (root / name).write_bytes(raw)
    manifest[key] = _record(name, raw)
    _manifest(root, manifest)


def _fixture(tmp_path):
    root = tmp_path / "encoder"
    root.mkdir()
    samples = [{"text": "", "expected_vector": [0., 0.]},
               {"text": "ALPHA alpha beta", "expected_vector": _reference({1: 2, 2: 1, 3: 1})},
               {"text": "café cafe\u0301 你好", "expected_vector": _reference({4: 1, 6: 1})}]
    weights = struct.pack("<21f", *IDF, *(v for row in ROWS for v in row))
    vocabulary = json.dumps(TERMS, ensure_ascii=False).encode()
    validation = json.dumps(samples).encode()
    provenance = dict.fromkeys(PROVENANCE_HASHES, "a" * 64)
    provenance.update(selected_method="A-frozen-s17", selection_protocol_id="b"*16,
                      training_protocol_id="c"*16, encoder_protocol_id="d"*16)
    manifest = {"schema_version": 1, "kind": "frozen-tfidf-svd-v1", "dtype": "float32-le",
                "dimension": 2, "vocabulary_terms": len(TERMS), "text_protocol": text_protocol(),
                "provenance": provenance, "fit": {"end_ms": 1000, "training_rows": 8, "document_count": 8},
                "vocabulary": _record("vocabulary.json", vocabulary), "weights": _record("weights.f32", weights),
                "validation": _record("validation.json", validation)}
    for name, raw in (("weights.f32", weights), ("vocabulary.json", vocabulary), ("validation.json", validation)):
        (root / name).write_bytes(raw)
    _manifest(root, manifest)
    return root, manifest, samples


@pytest.mark.parametrize("text,counts", [
    ("ALPHA alpha beta", {1: 2, 2: 1, 3: 1}), ("alpha x beta", {1: 1, 2: 1, 3: 1}),
    ("alpha OOV beta", {1: 1, 3: 1}), ("café cafe\u0301 你好", {4: 1, 6: 1}),
    ("123 under_score", {0: 1, 5: 1}), ("alpha---beta", {1: 1, 2: 1, 3: 1}),
    ("alpha alpha alpha", {1: 3}), ("", {}), ("a ! 😃 q", {}), ("entirelyunknown", {}),
])
def test_independent_transform_semantics(tmp_path, text, counts):
    root, _, _ = _fixture(tmp_path)
    assert load_content_encoder(root).encode(text) == pytest.approx(_reference(counts), abs=1e-6)


def test_immutable_runtime_and_approved_manifest(tmp_path):
    root, manifest, _ = _fixture(tmp_path)
    digest = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    runtime = load_content_encoder(root, expected_manifest_sha256=digest, expected_feature_fingerprint="a"*64)
    expected = runtime.encode("alpha beta")
    with pytest.raises(TypeError):
        runtime._vocabulary["beta"] = 0
    with pytest.raises(TypeError):
        runtime.provenance["encoder_sha256"] = "b"*64
    with pytest.raises(AttributeError):
        runtime.dimension = 3
    (root / "weights.f32").write_bytes(b"drift")
    assert runtime.encode("alpha beta") == expected
    with pytest.raises(ControlledLoadError) as error:
        load_content_encoder(root)
    assert error.value.code == "component_changed"


@pytest.mark.parametrize("key,value,code", [
    ("schema_version", True, "unsupported_format"), ("kind", "joblib", "unsupported_format"),
    ("dtype", "float64", "unsupported_format"), ("dimension", True, "resource_limit"),
    ("dimension", 129, "resource_limit"), ("vocabulary_terms", 20001, "resource_limit"),
    ("vocabulary_terms", 0, "resource_limit"), ("dimension", 3, "input_shape"),
    ("weights", None, "component_schema"), ("provenance", [], "component_schema"),
    ("extra", 1, "component_schema"),
])
def test_manifest_schema(tmp_path, key, value, code):
    root, manifest, _ = _fixture(tmp_path)
    manifest[key] = value
    _manifest(root, manifest)
    with pytest.raises(ControlledLoadError) as error:
        load_content_encoder(root)
    assert error.value.code == code


@pytest.mark.parametrize("key,value", [("lowercase", 1), ("sublinear_tf", False),
    ("token_pattern", r"\w+"), ("ngram_range", [True, 2]), ("norm", None), ("unicode_version", "0.0")])
def test_transform_protocol_cannot_drift(tmp_path, key, value):
    root, manifest, _ = _fixture(tmp_path)
    manifest["text_protocol"][key] = value
    _manifest(root, manifest)
    with pytest.raises(ControlledLoadError) as error:
        load_content_encoder(root)
    assert error.value.code == "component_schema"


@pytest.mark.parametrize("change", ["duplicate", "unsorted", "short-token", "uppercase", "trigram", "wrong-count", "non-string"])
def test_vocabulary_guards(tmp_path, change):
    root, manifest, _ = _fixture(tmp_path)
    terms = list(TERMS)
    if change == "duplicate": terms[1] = terms[0]
    elif change == "unsorted": terms.reverse()
    elif change == "wrong-count": terms.pop()
    else: terms[0] = {"short-token": "a", "uppercase": "ALPHA", "trigram": "alpha beta gamma", "non-string": 2}[change]
    _change(root, manifest, "vocabulary", json.dumps(terms).encode())
    with pytest.raises(ControlledLoadError):
        load_content_encoder(root)


@pytest.mark.parametrize("index,value", [(0, float("nan")), (0, float("inf")), (0, .9), (0, 33.),
                                       (7, float("nan")), (7, float("inf")), (7, 1.1)])
def test_finite_bounded_weights(tmp_path, index, value):
    root, manifest, _ = _fixture(tmp_path)
    weights = list(struct.unpack("<21f", (root / "weights.f32").read_bytes()))
    weights[index] = value
    _change(root, manifest, "weights", struct.pack("<21f", *weights))
    with pytest.raises(ControlledLoadError) as error:
        load_content_encoder(root)
    assert error.value.code == "non_finite"


@pytest.mark.parametrize("raw", [b'{"schema_version":1,"schema_version":1}', b'{"bad":NaN}',
    b'['*33+b'0'+b']'*33, '{"bad":"x"}'.encode("utf-16"), b'\xff'], ids=["duplicate", "nan", "depth", "utf16", "utf8"])
def test_portable_json_guards(tmp_path, raw):
    root, _, _ = _fixture(tmp_path)
    (root / "manifest.json").write_bytes(raw)
    with pytest.raises(ControlledLoadError) as error:
        load_content_encoder(root)
    assert error.value.code == "invalid_json"


@pytest.mark.parametrize("text,code", [(None, "input_shape"), (b"alpha", "input_shape"),
    ("\ud800", "input_shape"), ("a"*(MAX_TEXT_CHARS+1), "resource_limit"),
    ("aa "*(MAX_TOKENS+1), "resource_limit")], ids=["none", "bytes", "surrogate", "characters", "tokens"])
def test_text_resource_limits(tmp_path, text, code):
    root, _, _ = _fixture(tmp_path)
    with pytest.raises(ControlledLoadError) as error:
        load_content_encoder(root).encode(text)
    assert error.value.code == code


def test_exact_text_limits_and_unscaled_tiny_projection(tmp_path):
    root, manifest, _ = _fixture(tmp_path)
    runtime = load_content_encoder(root)
    assert runtime.encode("a" * MAX_TEXT_CHARS) == (0., 0.)
    assert runtime.encode("aa " * MAX_TOKENS) == (0., 0.)
    weights = struct.pack("<21f", *IDF, *(2e-8 for _ in range(14)))
    _change(root, manifest, "weights", weights)
    samples = [{"text": "", "expected_vector": [0., 0.]}, {"text": "alpha", "expected_vector": [2e-8, 2e-8]}]
    _change(root, manifest, "validation", json.dumps(samples).encode())
    result = load_content_encoder(root).encode("alpha")
    assert result == pytest.approx([2e-8, 2e-8], abs=1e-15)


@pytest.mark.parametrize("change", ["wrong-vector", "shape", "boolean", "huge-int", "no-empty", "no-signal", "too-many"])
def test_reference_replay_is_mandatory(tmp_path, change):
    root, manifest, samples = _fixture(tmp_path)
    if change == "wrong-vector": samples[1]["expected_vector"] = [1., 0.]
    elif change == "shape": samples[1]["expected_vector"] = [1.]
    elif change == "boolean": samples[1]["expected_vector"] = [True, 0.]
    elif change == "huge-int": samples[1]["expected_vector"] = [10**400, 0.]
    elif change == "no-empty": samples.pop(0)
    elif change == "no-signal": samples = [samples[0], samples[0]]
    else: samples = samples * 6
    _change(root, manifest, "validation", json.dumps(samples).encode())
    with pytest.raises(ControlledLoadError):
        load_content_encoder(root)


@pytest.mark.parametrize("change", ["extra-file", "missing-file", "path", "size", "file-symlink", "directory-symlink", "oversize"])
def test_files_and_paths_are_closed(tmp_path, monkeypatch, change):
    root, manifest, _ = _fixture(tmp_path)
    if change == "extra-file": (root / "encoder.joblib").write_bytes(b"not executed")
    elif change == "missing-file": (root / "validation.json").unlink()
    elif change == "path": manifest["weights"]["path"] = "../weights.f32"
    elif change == "size": manifest["weights"]["size_bytes"] -= 1
    elif change.endswith("symlink"):
        monkeypatch.setattr(Path, "is_symlink", lambda p: p == (root if change == "directory-symlink" else root / "weights.f32"))
    else:
        import evorec.infrastructure.content_encoder as module
        monkeypatch.setattr(module, "MAX_JSON_BYTES", 8)
    _manifest(root, manifest)
    with pytest.raises(ControlledLoadError):
        load_content_encoder(root)


@pytest.mark.parametrize("key", [*BINDING_KEYS, "dimension"])
def test_every_feature_binding_is_checked(tmp_path, key):
    root, manifest, _ = _fixture(tmp_path)
    runtime = load_content_encoder(root)
    features = R06Features(2, (), "f"*64, "a"*64, 0, MappingProxyType(dict(manifest["provenance"])), {}, (), b"", b"")
    runtime.check_features(features)
    provenance = dict(features.provenance)
    provenance[key] = "e" * (16 if key.endswith("protocol_id") else 64)
    changed = replace(features, dimension=3) if key == "dimension" else replace(features, provenance=provenance)
    with pytest.raises(ControlledLoadError) as error:
        runtime.check_features(changed)
    assert error.value.code == "component_changed"


def test_manifest_and_feature_pins_and_fit_bounds(tmp_path):
    root, manifest, _ = _fixture(tmp_path)
    for keyword in ("expected_manifest_sha256", "expected_feature_fingerprint"):
        with pytest.raises(ControlledLoadError) as error:
            load_content_encoder(root, **{keyword: "e"*64})
        assert error.value.code == "component_changed"
    manifest["fit"]["document_count"] = 9
    _manifest(root, manifest)
    with pytest.raises(ControlledLoadError) as error:
        load_content_encoder(root)
    assert error.value.code == "component_schema"


@pytest.mark.parametrize("key", [*PROVENANCE_HASHES, "selection_protocol_id", "training_protocol_id", "encoder_protocol_id", "selected_method"])
def test_provenance_schema_is_closed_and_validated(tmp_path, key):
    root, manifest, _ = _fixture(tmp_path)
    manifest["provenance"][key] = "B-frozen-s17" if key == "selected_method" else "not-a-digest"
    _manifest(root, manifest)
    with pytest.raises(ControlledLoadError) as error:
        load_content_encoder(root)
    assert error.value.code == "component_schema"


@pytest.mark.parametrize("key,value", [("end_ms", -1), ("end_ms", True), ("training_rows", True),
    ("training_rows", 10000001), ("document_count", 200001)])
def test_fit_record_resource_limits(tmp_path, key, value):
    root, manifest, _ = _fixture(tmp_path)
    manifest["fit"][key] = value
    _manifest(root, manifest)
    with pytest.raises(ControlledLoadError):
        load_content_encoder(root)


def test_total_reference_limit_and_invalid_feature_type(tmp_path, monkeypatch):
    root, _, _ = _fixture(tmp_path)
    runtime = load_content_encoder(root)
    with pytest.raises(ControlledLoadError) as error:
        runtime.check_features(None)
    assert error.value.code == "input_shape"
    import evorec.infrastructure.content_encoder as module
    monkeypatch.setattr(module, "MAX_REFERENCE_CHARS", 3)
    with pytest.raises(ControlledLoadError) as error:
        load_content_encoder(root)
    assert error.value.code == "resource_limit"


def test_service_subprocess_forbids_executable_and_research_imports(tmp_path):
    root, _, _ = _fixture(tmp_path)
    code = """
import importlib.abc, sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'torch', 'numpy', 'scipy', 'sklearn', 'joblib', 'pickle'}:
            raise AssertionError('forbidden import: ' + fullname)
sys.meta_path.insert(0, Deny())
from evorec.infrastructure.content_encoder import load_content_encoder
r = load_content_encoder(sys.argv[1])
assert len(r.encode('alpha beta')) == 2
assert r.encode('') == (0., 0.)
"""
    subprocess.run([sys.executable, "-c", code, str(root)], check=True, capture_output=True, text=True)


def test_cli_requires_approval_and_reports_no_activation(tmp_path):
    root, _, _ = _fixture(tmp_path)
    digest = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    script = str(Path(__file__).resolve().parents[1] / "scripts/load_content_encoder.py")
    base = [sys.executable, script, str(root), "--expected-manifest-sha256"]
    good = subprocess.run([*base, digest], capture_output=True, text=True)
    assert good.returncode == 0
    result = json.loads(good.stdout)
    assert result["component_only"] and not result["activated"] and not result["feature_binding_checked"]
    bad = subprocess.run([*base, "e"*64], capture_output=True, text=True)
    assert bad.returncode == 1 and json.loads(bad.stdout)["code"] == "component_changed"
    pair = subprocess.run([*base, digest, "--features-component", str(root)], capture_output=True, text=True)
    assert pair.returncode == 2
