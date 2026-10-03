"""Independent numerical and hostile-input checks; no research dependencies."""

import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from evorec.infrastructure.model_runtime import ControlledLoadError
from evorec.infrastructure.bundle import BundleValidationError, validate_bundle
from evorec.infrastructure.residual_ranker import (
    PROVENANCE_HASHES, SCALAR_NAMES, load_residual_ranker,
)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _record(name, raw):
    return {"path": name, "size_bytes": len(raw), "sha256": _digest(raw)}


def _f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _reference(context, candidate, scalars):
    base = 4 * (scalars[1] + scalars[2])
    if context == 0:
        return _f32(base)
    # Explicit one-dimensional feature formula, independent of runtime helpers.
    x = [context, candidate, context * candidate, abs(context - candidate), *scalars]
    gelu = lambda v: v * .5 * (1 + math.erf(v / math.sqrt(2)))
    h0 = gelu(sum(_f32((i + 1) * .01) * v for i, v in enumerate(x)) + _f32(.25))
    h1 = gelu(sum(_f32((i + 1) * -.02) * v for i, v in enumerate(x)) - _f32(.1))
    b = gelu(.5 * h0 + _f32(-.2) * h1 + _f32(.1))
    return _f32(base + 4 * math.tanh(.75 * b - _f32(.05)))


def _fixture(tmp_path):
    root = tmp_path / "component"
    root.mkdir()
    values = ([.01 * (i + 1) for i in range(12)]
              + [-.02 * (i + 1) for i in range(12)]
              + [.25, -.1, .5, -.2, .1, .75, -.05])
    weights = struct.pack(f"<{len(values)}f", *values)
    scalars = [[.25, .5, .25, .1, 1., .7, .5, .8], [.5, .25, .5, .2, 0., .8, .5, .8]]
    samples = []
    for context in (1., 0.):
        scores = [_reference(context, c, s) for c, s in zip((.25, .5), scalars, strict=True)]
        samples.append({"context": [context], "candidates": [[.25], [.5]], "scalars": scalars,
                        "expected_scores": scores,
                        "expected_top20": sorted(range(2), key=lambda i: -scores[i])})
    validation = json.dumps(samples).encode()
    provenance = dict.fromkeys(PROVENANCE_HASHES, "a" * 64)
    provenance.update(selected_method="A-frozen-s17", selection_protocol_id="b" * 16,
                      training_protocol_id="c" * 16)
    manifest = {
        "schema_version": 1, "kind": "residual-list-mlp-v1", "dtype": "float32-le",
        "dimension": 1, "hidden": 2, "bottleneck": 1, "base_scale": 8, "residual_scale": 4,
        "scalar_names": list(SCALAR_NAMES), "provenance": provenance,
        "weights": _record("weights.f32", weights), "validation": _record("validation.json", validation),
    }
    (root / "weights.f32").write_bytes(weights)
    (root / "validation.json").write_bytes(validation)
    _manifest(root, manifest)
    return root, manifest, samples


def _manifest(root, manifest):
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _change_file(root, manifest, name, raw):
    (root / name).write_bytes(raw)
    key = "weights" if name == "weights.f32" else "validation"
    manifest[key] = _record(name, raw)
    _manifest(root, manifest)


def test_independent_numerics_empty_history_and_immutable_runtime(tmp_path):
    root, _, samples = _fixture(tmp_path)
    digest = _digest((root / "manifest.json").read_bytes())
    runtime = load_residual_ranker(root, expected_manifest_sha256=digest)
    assert runtime.validation_samples_checked == 2
    for sample in samples:
        scores = runtime.score(sample["context"], sample["candidates"], sample["scalars"])
        assert scores == tuple(sample["expected_scores"])
    # Equal base ranks stay tied for empty context, despite different candidate vectors.
    assert runtime.score([0.], [[.25], [.5]], samples[0]["scalars"]) == (3., 3.)
    with pytest.raises(TypeError):
        runtime.provenance["selected_method"] = "other"
    (root / "weights.f32").write_bytes(b"changed")
    assert runtime.score([1.], [[.25]], samples[0]["scalars"][:1]) == (samples[0]["expected_scores"][0],)
    with pytest.raises(ControlledLoadError, match="hash mismatch"):
        load_residual_ranker(root)


@pytest.mark.parametrize("key,value,code", [
    ("kind", "pickle", "unsupported_format"), ("dtype", "float64", "unsupported_format"),
    ("schema_version", True, "unsupported_format"), ("dimension", True, "resource_limit"),
    ("dimension", 129, "resource_limit"), ("hidden", 257, "resource_limit"),
    ("bottleneck", 129, "resource_limit"), ("base_scale", 0, "component_schema"),
    ("base_scale", 1001, "component_schema"), ("residual_scale", -1, "component_schema"),
    ("residual_scale", 10**400, "non_finite"), ("weights", None, "component_schema"),
    ("scalar_names", list(reversed(SCALAR_NAMES)), "component_schema"),
])
def test_schema_and_resource_guards(tmp_path, key, value, code):
    root, manifest, _ = _fixture(tmp_path)
    manifest[key] = value
    _manifest(root, manifest)
    with pytest.raises(ControlledLoadError) as error:
        load_residual_ranker(root)
    assert error.value.code == code


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff', b'{}', b'[' * 1200])
def test_strict_json(tmp_path, raw):
    root, _, _ = _fixture(tmp_path)
    (root / "manifest.json").write_bytes(raw)
    with pytest.raises(ControlledLoadError):
        load_residual_ranker(root)


def test_manifest_pin_and_unknown_fields(tmp_path):
    root, manifest, _ = _fixture(tmp_path)
    with pytest.raises(ControlledLoadError) as error:
        load_residual_ranker(root, expected_manifest_sha256="0" * 64)
    assert error.value.code == "component_changed"
    manifest["arbitrary_code"] = "eval"
    _manifest(root, manifest)
    with pytest.raises(ControlledLoadError) as error:
        load_residual_ranker(root)
    assert error.value.code == "component_schema"


def test_component_cannot_be_loaded_as_a_complete_online_bundle(tmp_path):
    root, _, _ = _fixture(tmp_path)
    with pytest.raises(BundleValidationError):
        validate_bundle(tmp_path, root)


def test_validation_cli_reports_failure_and_never_activation(tmp_path, capsys):
    from scripts.load_residual_ranker import main
    root, _, _ = _fixture(tmp_path)
    assert main([str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["activated"] is False
    assert main([str(root), "--expected-manifest-sha256", "0" * 64]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "component_changed"


@pytest.mark.parametrize("field,value", [
    ("path", "../weights.f32"), ("path", "weights.pt"), ("size_bytes", 124 + 4),
    ("sha256", "0" * 64), ("sha256", "not-a-digest"),
])
def test_weight_record_rejected(tmp_path, field, value):
    root, manifest, _ = _fixture(tmp_path)
    manifest["weights"][field] = value
    _manifest(root, manifest)
    with pytest.raises(ControlledLoadError):
        load_residual_ranker(root)


@pytest.mark.parametrize("value", [math.inf, math.nan, 1_000_001.])
def test_non_finite_or_extreme_weights_even_with_correct_hash(tmp_path, value):
    root, manifest, _ = _fixture(tmp_path)
    raw = (root / "weights.f32").read_bytes()
    _change_file(root, manifest, "weights.f32", struct.pack("<f", value) + raw[4:])
    with pytest.raises(ControlledLoadError) as error:
        load_residual_ranker(root)
    assert error.value.code == "non_finite"


def test_weight_and_golden_numerical_drift_are_not_accepted(tmp_path):
    root, manifest, samples = _fixture(tmp_path)
    raw = (root / "weights.f32").read_bytes()
    _change_file(root, manifest, "weights.f32", raw[:-4] + struct.pack("<f", 1.))
    with pytest.raises(ControlledLoadError) as error:
        load_residual_ranker(root)
    assert error.value.code == "validation_mismatch"
    _change_file(root, manifest, "weights.f32", raw)
    samples[0]["expected_top20"].reverse()
    _change_file(root, manifest, "validation.json", json.dumps(samples).encode())
    with pytest.raises(ControlledLoadError) as error:
        load_residual_ranker(root)
    assert error.value.code == "validation_mismatch"


@pytest.mark.parametrize("samples", [[], [{}] * 5, [{"context": []}]])
def test_reference_count_and_shape_limits(tmp_path, samples):
    root, manifest, _ = _fixture(tmp_path)
    _change_file(root, manifest, "validation.json", json.dumps(samples).encode())
    with pytest.raises(ControlledLoadError):
        load_residual_ranker(root)


def test_missing_unlisted_file_and_symlink_guard(tmp_path, monkeypatch):
    root, _, _ = _fixture(tmp_path)
    (root / "executable.pt").write_bytes(b"untrusted")
    with pytest.raises(ControlledLoadError) as error:
        load_residual_ranker(root)
    assert error.value.code == "unsupported_format"
    (root / "executable.pt").unlink()
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == "weights.f32" or original(path))
    with pytest.raises(ControlledLoadError) as error:
        load_residual_ranker(root)
    assert error.value.code == "unsafe_path"


def test_oversized_file_rejected_before_read(tmp_path, monkeypatch):
    root, _, _ = _fixture(tmp_path)
    monkeypatch.setattr("evorec.infrastructure.residual_ranker.MAX_JSON_BYTES", 1)
    with pytest.raises(ControlledLoadError) as error:
        load_residual_ranker(root)
    assert error.value.code == "resource_limit"


@pytest.mark.parametrize("context,candidates,scalars", [
    ([], [[1]], [[0] * 8]), ([1], [], []), ([1], [[1]] * 401, [[0] * 8] * 401),
    ([1], [[1]], []), ([True], [[1]], [[0] * 8]), ([math.nan], [[1]], [[0] * 8]),
    ([1], [[1, 2]], [[0] * 8]), ([1], [[1]], [[0] * 7]),
    ([0], [[math.inf]], [[0] * 8]), ([1], "x", [[0] * 8]),
])
def test_score_input_guards_including_empty_history(tmp_path, context, candidates, scalars):
    root, _, _ = _fixture(tmp_path)
    runtime = load_residual_ranker(root)
    with pytest.raises(ControlledLoadError):
        runtime.score(context, candidates, scalars)


def test_service_load_runs_with_neural_and_array_imports_forbidden(tmp_path):
    root, _, _ = _fixture(tmp_path)
    code = '''
import builtins,sys
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name.split('.')[0] in {'torch','numpy','sklearn','joblib','pickle'}:
        raise AssertionError('executable/research import: '+name)
    return original(name,*args,**kwargs)
builtins.__import__=guarded
from evorec.infrastructure.residual_ranker import load_residual_ranker
assert load_residual_ranker(sys.argv[1]).validation_samples_checked == 2
'''
    result = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
