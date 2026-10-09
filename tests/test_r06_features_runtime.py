"""Label-free frozen-feature boundaries and independent numerical references."""

import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest

from evorec.infrastructure.r06_features import PROVENANCE_HASHES, _ids, load_r06_features
from evorec.infrastructure.residual_ranker import SCALAR_NAMES, ControlledLoadError


def _f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _record(name, raw):
    return {"path": name, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _save(root, manifest):
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _update(root, manifest, key, value):
    name = manifest[key]["path"]
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    (root / name).write_bytes(raw)
    manifest[key] = _record(name, raw)
    _save(root, manifest)


def _fixture(tmp_path):
    root = tmp_path / "features"
    root.mkdir()
    ids = ("a", "b", "c", "zero")
    vectors = struct.pack("<8f", 1, 0, 0, 1, .6, .8, 0, 0)
    metadata = [{"item_id": item, "first_seen_ms": 1, "training_item": item in ("a", "b"),
                 "prior_strength": 1. if item == "a" else 0.} for item in ids]
    samples = []
    age = _f32(math.log1p(1) / math.log1p(3650))
    length = _f32(math.log1p(1) / math.log1p(50))
    for represented in (True, False):
        samples.append({
            "history": ["a"] if represented else [], "seen": ["a"] if represented else [],
            "timestamp_ms": 86400001, "collaborative": ["b", "c"], "content": ["c", "b"],
            "expected_items": ["b", "c"], "expected_context": [1., 0.] if represented else [0., 0.],
            "expected_scalars": [
                [0., 1., _f32(61/62), 0., 0., age, length if represented else 0., 1. if represented else 0.],
                [_f32(.6) if represented else 0., _f32(61/62), 1., 0., 1., age,
                 length if represented else 0., 1. if represented else 0.],
            ],
        })
    provenance = dict.fromkeys(PROVENANCE_HASHES, "a" * 64)
    digest = hashlib.sha256(vectors)
    digest.update(json.dumps(ids).encode())
    provenance.update(feature_fingerprint=digest.hexdigest(), selection_protocol_id="b" * 16,
                      training_protocol_id="c" * 16)
    items, validation = json.dumps(metadata).encode(), json.dumps(samples).encode()
    manifest = {
        "schema_version": 1, "kind": "r06-pool-features-v1", "dtype": "float32-le",
        "dimension": 2, "item_count": 4, "history_decay": .8, "history_limit": 50, "rrf_constant": 60,
        "scalar_names": list(SCALAR_NAMES), "provenance": provenance,
        "items": _record("items.json", items), "vectors": _record("vectors.f32", vectors),
        "validation": _record("validation.json", validation),
    }
    for name, raw in (("items.json", items), ("vectors.f32", vectors), ("validation.json", validation)):
        (root / name).write_bytes(raw)
    _save(root, manifest)
    return root, manifest, samples, metadata


def test_independent_reference_scalars_order_and_empty_history(tmp_path):
    root, manifest, samples, _ = _fixture(tmp_path)
    runtime = load_r06_features(root, expected_feature_fingerprint=manifest["provenance"]["feature_fingerprint"])
    assert runtime.validation_samples_checked == 2
    for sample in samples:
        pool = runtime.build_pool(*(sample[k] for k in ("history", "seen", "timestamp_ms", "collaborative", "content")))
        assert pool.item_ids == tuple(sample["expected_items"])
        assert pool.context == tuple(sample["expected_context"])
        assert pool.scalars == tuple(tuple(row) for row in sample["expected_scalars"])
    # b is in training through a low rating, even though it has zero positive prior.
    assert runtime.build_pool([], [], 2, ["b"], []).scalars[0][4] == 0
    (root / "vectors.f32").write_bytes(b"changed")
    assert runtime.vector("a") == (1., 0.)
    with pytest.raises(ControlledLoadError):
        load_r06_features(root)


def test_unknown_and_zero_history_preserve_distance_and_length(tmp_path):
    root, _, _, _ = _fixture(tmp_path)
    runtime = load_r06_features(root)
    history = ["a", "unknown", "b"]
    pool = runtime.build_pool(history, history, 86400001, ["c"], [])
    norm = math.sqrt(.64**2 + 1)
    assert pool.context == pytest.approx((.64/norm, 1/norm), abs=1e-6)
    assert pool.scalars[0][6] == pytest.approx(math.log1p(3)/math.log1p(50), abs=1e-6)
    assert pool.scalars[0][7] == pytest.approx(norm/1.64, abs=1e-6)
    missing = runtime.build_pool(["unknown", "zero"], ["unknown", "zero"], 2, ["b"], [])
    assert missing.context == (0., 0.) and missing.scalars[0][7] == 0.
    assert missing.scalars[0][6] > 0  # Not a cold user, but unrepresented history.


def test_provider_overlap_is_unique_sorted_but_rank_positions_are_kept(tmp_path):
    root, _, _, _ = _fixture(tmp_path)
    runtime = load_r06_features(root)
    pool = runtime.build_pool([], [], 2, ["c", "b"], ["b"])
    assert pool.item_ids == ("b", "c")
    assert pool.scalars[0][1:3] == (_f32(61/62), 1.)
    assert pool.scalars[1][1:3] == (1., 0.)
    assert runtime.build_pool([], [], 2, [], []).item_ids == ()


def test_shared_id_validator_preserves_sequence_tuple_and_duplicate_contract():
    values = ["a", "b"]
    result = _ids(values, 2)
    assert type(result) is tuple and result == ("a", "b")
    duplicate = ("a", "a")
    assert _ids(duplicate, 2) is duplicate  # Non-unique validation preserves tuple inputs.
    assert _ids(duplicate, 2, unique=False) == ("a", "a")
    with pytest.raises(ControlledLoadError) as error:
        _ids(("a", "a"), 2, unique=True)
    assert error.value.code == "input_shape"


@pytest.mark.parametrize("factory", [
    pytest.param(lambda: None, id="none"),
    pytest.param(lambda: "a", id="string"),
    pytest.param(lambda: b"a", id="bytes"),
    pytest.param(lambda: {"a"}, id="set"),
    pytest.param(lambda: (item for item in ("a",)), id="generator"),
])
def test_shared_id_validator_requires_bounded_sequence_shape(factory):
    with pytest.raises(ControlledLoadError) as error:
        _ids(factory(), 1)
    assert error.value.code == "input_shape"


def test_shared_id_validator_rejects_oversized_sequence():
    with pytest.raises(ControlledLoadError) as error:
        _ids(["a", "b"], 1)
    assert error.value.code == "input_shape"


@pytest.mark.parametrize("value", ["", "   ", "x" * 129, None, 7])
def test_shared_id_validator_rejects_invalid_ids(value):
    with pytest.raises(ControlledLoadError) as error:
        _ids([value], 1)
    assert error.value.code == "input_shape"


def test_shared_id_validator_visits_every_valid_string_subclass():
    visited = []

    class TrackedID(str):
        def strip(self, chars=None):
            visited.append(self)
            return super().strip(chars)

    values = (TrackedID("a"), TrackedID("b"), TrackedID("c"))
    assert _ids(values, len(values)) == values
    assert tuple(visited) == values


@pytest.mark.parametrize("history,seen,time,cf,content", [
    (["a"], [], 2, ["b"], []), ([], ["b"], 2, ["b"], []), ([], [], 1, ["b"], []),
    ([], [], 0, ["b"], []), ([], [], 2, ["unknown"], []),
    ([], [], 2, ["b", "b"], []), ([], [], 2, [], ["c", "c"]),
    ([], [], True, ["b"], []), ([], [], -1, ["b"], []),
    ([], [], 253402300800000, ["b"], []), (["a"]*51, ["a"], 2, ["b"], []),
    ([], ["b"]*10001, 2, ["c"], []), ([], [], 2, ["c"]*201, []),
    ("a", ["a"], 2, ["b"], []), ([], [], 2, [True], []),
])
def test_request_boundaries_reject_seen_equal_time_duplicates_unknown_and_limits(tmp_path, history, seen, time, cf, content):
    root, _, _, _ = _fixture(tmp_path)
    runtime = load_r06_features(root)
    with pytest.raises(ControlledLoadError):
        runtime.build_pool(history, seen, time, cf, content)


@pytest.mark.parametrize("key,value", [
    ("kind", "joblib"), ("dtype", "float64"), ("schema_version", True),
    ("dimension", 129), ("item_count", 200001), ("history_decay", .9),
    ("history_limit", True), ("rrf_constant", 61), ("scalar_names", list(reversed(SCALAR_NAMES))),
])
def test_manifest_protocol_and_size_guards(tmp_path, key, value):
    root, manifest, _, _ = _fixture(tmp_path)
    manifest[key] = value
    _save(root, manifest)
    with pytest.raises(ControlledLoadError):
        load_r06_features(root)


@pytest.mark.parametrize("change", ["duplicate", "unsorted", "cold-prior", "flag", "timestamp", "nan-prior"])
def test_mapping_and_statistics_guards_with_updated_hash(tmp_path, change):
    root, manifest, _, rows = _fixture(tmp_path)
    if change == "duplicate": rows[1]["item_id"] = "a"
    elif change == "unsorted": rows.reverse()
    elif change == "cold-prior": rows[2]["prior_strength"] = .5
    elif change == "flag": rows[0]["training_item"] = 1
    elif change == "timestamp": rows[0]["first_seen_ms"] = -1
    else: rows[0]["prior_strength"] = math.nan
    _update(root, manifest, "items", rows)
    with pytest.raises(ControlledLoadError):
        load_r06_features(root)


@pytest.mark.parametrize("value", [math.nan, math.inf, 2., .5])
def test_non_finite_or_non_normalized_vectors_even_with_updated_hash(tmp_path, value):
    root, manifest, _, _ = _fixture(tmp_path)
    raw = (root / "vectors.f32").read_bytes()
    _update(root, manifest, "vectors", struct.pack("<f", value) + raw[4:])
    with pytest.raises(ControlledLoadError):
        load_r06_features(root)


def test_manifest_fingerprint_hash_shape_and_golden_drift(tmp_path):
    root, manifest, samples, _ = _fixture(tmp_path)
    with pytest.raises(ControlledLoadError):
        load_r06_features(root, expected_manifest_sha256="0"*64)
    with pytest.raises(ControlledLoadError):
        load_r06_features(root, expected_feature_fingerprint="0"*64)
    samples[0]["expected_scalars"][1][4] = 0.
    _update(root, manifest, "validation", samples)
    with pytest.raises(ControlledLoadError) as error:
        load_r06_features(root)
    assert error.value.code == "validation_mismatch"


def test_cross_component_mismatch_is_checked_before_scoring(tmp_path):
    root, _, _, _ = _fixture(tmp_path)
    runtime = load_r06_features(root)
    pool = runtime.build_pool([], [], 2, ["b"], [])
    wrong = SimpleNamespace(dimension=2, provenance={**runtime.provenance, "source_series_sha256": "0"*64},
                            score=lambda *_: pytest.fail("mismatched ranker must not execute"))
    with pytest.raises(ControlledLoadError):
        pool.score(wrong)
    correct = SimpleNamespace(dimension=2, provenance=runtime.provenance,
                              score=lambda context, candidates, scalars: (scalars[0][1],))
    assert pool.score(correct) == (1.,)
    assert runtime.build_pool([], [], 2, [], []).score(correct) == ()


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff',
                                b'{"number":'+b'1'*5000+b'}', b'['*5000+b'0'+b']'*5000,
                                '{"a":1}'.encode("utf-16")],
                         ids=["duplicate-key", "nan", "invalid-utf8", "oversized-int", "deep-nesting", "utf16"])
def test_strict_json_including_oversized_numeric_literal(tmp_path, raw):
    root, _, _, _ = _fixture(tmp_path)
    (root / "manifest.json").write_bytes(raw)
    with pytest.raises(ControlledLoadError) as error:
        load_r06_features(root)
    assert error.value.code == "invalid_json"


@pytest.mark.parametrize("depth", [32, 33], ids=["at-depth-limit", "above-depth-limit"])
def test_json_depth_limit_is_explicit_not_decoder_dependent(depth):
    from evorec.infrastructure.r06_features import _json
    raw = b"["*depth+b"0"+b"]"*depth
    if depth == 33:
        with pytest.raises(ControlledLoadError) as error:
            _json(raw)
        assert error.value.code == "invalid_json"
    else:
        value = _json(raw)
        for _ in range(depth):
            assert len(value) == 1
            value = value[0]
        assert value == 0


@pytest.mark.parametrize("value", ["[{}]"*100, "\\\"[{}]"*100, "\\\\[{}]"*100, "中文\"\\[{}]"*100],
                         ids=["quoted-brackets", "escaped-quote", "escaped-backslash", "unicode-escapes"])
def test_json_depth_guard_ignores_brackets_and_escaped_quotes_in_strings(value):
    from evorec.infrastructure.r06_features import _json
    assert _json(json.dumps({"text": value}, ensure_ascii=False).encode()) == {"text": value}


def test_overdeep_json_is_rejected_before_platform_decoder(monkeypatch):
    from evorec.infrastructure import r06_features
    monkeypatch.setattr(r06_features, "_ranker_json", lambda *_: pytest.fail("overdeep input reached decoder"))
    with pytest.raises(ControlledLoadError) as error:
        r06_features._json(b"["*33+b"0"+b"]"*33)
    assert error.value.code == "invalid_json"


def test_paths_symlink_extra_file_resource_limit_and_vector_shape(tmp_path, monkeypatch):
    root, manifest, _, _ = _fixture(tmp_path)
    manifest["vectors"]["path"] = "../escape.f32"
    _save(root, manifest)
    with pytest.raises(ControlledLoadError) as error:
        load_r06_features(root)
    assert error.value.code == "unsafe_path"
    manifest["vectors"]["path"] = "vectors.f32"
    manifest["vectors"]["size_bytes"] += 4
    _save(root, manifest)
    with pytest.raises(ControlledLoadError) as error:
        load_r06_features(root)
    assert error.value.code == "input_shape"
    manifest["vectors"]["size_bytes"] -= 4
    _save(root, manifest)
    original = Path.is_symlink
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "is_symlink", lambda p: p.name == "vectors.f32" or original(p))
        with pytest.raises(ControlledLoadError) as error:
            load_r06_features(root)
        assert error.value.code == "unsafe_path"
    (root / "unexpected.pt").write_bytes(b"untrusted")
    with pytest.raises(ControlledLoadError) as error:
        load_r06_features(root)
    assert error.value.code == "unsupported_format"
    (root / "unexpected.pt").unlink()
    monkeypatch.setattr("evorec.infrastructure.r06_features.MAX_JSON_BYTES", 1)
    with pytest.raises(ControlledLoadError) as error:
        load_r06_features(root)
    assert error.value.code == "resource_limit"


def test_service_component_load_without_research_or_executable_imports(tmp_path):
    root, _, _, _ = _fixture(tmp_path)
    code = '''
import builtins,sys
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name.split('.')[0] in {'torch','numpy','sklearn','joblib','pickle'}:
        raise AssertionError('research/executable import: '+name)
    return original(name,*args,**kwargs)
builtins.__import__=guarded
from evorec.infrastructure.r06_features import load_r06_features
assert load_r06_features(sys.argv[1]).item_ids == ('a','b','c','zero')
'''
    result = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_cli_requires_approved_digest_and_reports_controlled_failure(tmp_path, capsys):
    from scripts.load_r06_features import main
    root, _, _, _ = _fixture(tmp_path)
    digest = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    assert main([str(root), "--expected-manifest-sha256", digest]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "passed" and result["item_count"] == 4
    assert not result["activated"] and not result["ranker_binding_checked"]
    assert main([str(root), "--expected-manifest-sha256", "0"*64]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "component_changed"
    with pytest.raises(SystemExit) as error:
        main([str(root)])
    assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        main([str(root), "--expected-manifest-sha256", digest, "--ranker-component", str(root)])
    assert error.value.code == 2
