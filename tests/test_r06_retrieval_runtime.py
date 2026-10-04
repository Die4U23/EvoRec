"""Independent synthetic ordering/filters and hostile controlled-file boundaries."""

from dataclasses import replace
import hashlib
import json
import math
import struct
import subprocess
import sys
from types import MappingProxyType

import pytest

from evorec.infrastructure.r06_features import PROVENANCE_HASHES, load_r06_features
from evorec.infrastructure import r06_retrieval as module
from evorec.infrastructure.r06_retrieval import BINDING_KEYS, _content_score, load_r06_retrieval, retrieval_protocol
from evorec.infrastructure.residual_ranker import ControlledLoadError, SCALAR_NAMES

IDS = ("a", "b", "c", "d", "e", "zero")
COUNTS = [4., 2., 1., 1., 0., 3.]
NEIGHBORS = {"a": (("b", .5), ("c", .25)), "b": (("c", .75), ("d", .25)),
             "c": (("b", .75), ("a", .25)), "d": (("b", .25),)}
FIT = {"end_ms": 10, "training_rows": 16, "positive_rating_min": 4}


def test_content_arithmetic_is_explicit_f32_not_host_blas_or_double():
    # Each product is exactly representable; float32 accumulation loses the small
    # contribution, whereas double accumulation preserves it. No epsilon ties.
    assert _content_score((1., 1., 1.), (1., 2**-25, -1.)) == 0.
    assert sum((1., 2**-25, -1.)) == 2**-25
    # Product rounding too is required (not merely a final float32 cast).
    value = struct.unpack("<f", struct.pack("<f", .6))[0]
    expected = struct.unpack("<f", struct.pack("<f", value * value))[0]
    assert _content_score((value,), (value,)) == expected


def _record(name, raw):
    return {"path": name, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _save(root, manifest):
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _change(root, manifest, key, raw):
    name = manifest[key]["path"]
    (root / name).write_bytes(raw)
    manifest[key] = _record(name, raw)
    _save(root, manifest)


def _features(tmp_path):
    root = tmp_path / "features"
    root.mkdir()
    vectors = struct.pack("<12f", 1, 0, 0, 1, .6, .8, -1, 0, 1, 0, 0, 0)
    metadata = [{"item_id": item, "first_seen_ms": 1 if item != "e" else 10,
                 "training_item": item != "e", "prior_strength": math.log1p(n) / math.log(5)}
                for item, n in zip(IDS, COUNTS, strict=True)]
    digest = hashlib.sha256(vectors)
    digest.update(json.dumps(IDS).encode())
    provenance = dict.fromkeys(PROVENANCE_HASHES, "a" * 64)
    provenance.update(feature_fingerprint=digest.hexdigest(), selection_protocol_id="b"*16, training_protocol_id="c"*16)
    sample = {"history": [], "seen": [], "timestamp_ms": 0, "collaborative": [], "content": [],
              "expected_items": [], "expected_context": [0., 0.], "expected_scalars": []}
    manifest = {"schema_version": 1, "kind": "r06-pool-features-v1", "dtype": "float32-le",
                "dimension": 2, "item_count": len(IDS), "history_decay": .8, "history_limit": 50,
                "rrf_constant": 60, "scalar_names": list(SCALAR_NAMES), "provenance": provenance}
    for key, name, raw in (("items", "items.json", json.dumps(metadata).encode()),
                           ("vectors", "vectors.f32", vectors), ("validation", "validation.json", json.dumps([sample]).encode())):
        (root / name).write_bytes(raw)
        manifest[key] = _record(name, raw)
    _save(root, manifest)
    return load_r06_features(root)


def _samples():
    # Hand calculated blend: b=.25+.75*log(3)/log(5), zero=.75*log(4)/log(5),
    # c=.125+.75*log(2)/log(5), d=.75*log(2)/log(5). Content includes negative
    # finite scores before the unrepresented popularity fill.
    return [{"history": ["a"], "seen": ["a"], "timestamp_ms": 11,
             "collaborative": ["b", "zero", "c", "d"], "content": ["e", "c", "b", "d", "zero"]},
            {"history": [], "seen": [], "timestamp_ms": 11,
             "collaborative": ["a", "zero", "b", "c", "d"], "content": ["a", "zero", "b", "c", "d"]}]


def _fixture(tmp_path):
    features = _features(tmp_path)
    root = tmp_path / "retrieval"
    root.mkdir()
    offsets, edges = [0], bytearray()
    for item in IDS:
        for other, similarity in NEIGHBORS.get(item, ()):
            edges.extend(struct.pack("<Id", IDS.index(other), similarity))
        offsets.append(len(edges) // 12)
    graph = struct.pack("<7I", *offsets) + edges
    manifest = {"schema_version": 1, "kind": "r06-cf-centroid-v1", "graph_dtype": "csr-u32-f64-le",
                "item_count": len(IDS), "edge_count": len(edges) // 12, "protocol": retrieval_protocol(), "fit": FIT.copy(),
                "provenance": {**features.provenance, "features_manifest_sha256": features.manifest_sha256,
                               "selected_method": "A-frozen-s17"}}
    for key, name, raw in (("statistics", "statistics.json", json.dumps(COUNTS).encode()),
                           ("neighbors", "neighbors.bin", graph), ("validation", "validation.json", json.dumps(_samples()).encode())):
        (root / name).write_bytes(raw)
        manifest[key] = _record(name, raw)
    _save(root, manifest)
    return root, manifest, features


def test_independent_full_orders_and_pool(tmp_path):
    root, _, features = _fixture(tmp_path)
    runtime = load_r06_retrieval(root, features)
    assert runtime.edge_count == 7 and runtime.validation_samples_checked == 2
    for sample in _samples():
        actual = runtime.retrieve(sample["history"], sample["seen"], sample["timestamp_ms"])
        assert actual.collaborative == tuple(sample["collaborative"])
        assert actual.content == tuple(sample["content"])
        assert runtime.build_pool(sample["history"], sample["seen"], sample["timestamp_ms"]).item_ids == tuple(sorted(
            set(actual.collaborative) | set(actual.content)))
    result = runtime.retrieve(["a"], ["a"], 11)
    assert result.collaborative_personalized == 2 and result.content_personalized == 4


def test_full_seen_and_strict_availability_and_unknown_distance(tmp_path):
    root, _, f = _fixture(tmp_path)
    r = load_r06_retrieval(root, f)
    at_boundary = r.retrieve(["a"], {"a", "b"}, 10)
    assert "b" not in at_boundary.collaborative + at_boundary.content  # low-rating seen exclusion
    assert "e" not in at_boundary.content  # first seen == request is not available
    assert r.retrieve(["a", "ghost"], {"a", "ghost"}, 11).content == r.retrieve(["a"], {"a"}, 11).content
    for history, seen in (([], []), (["ghost"], ["ghost"]), (["zero"], ["zero"])):
        result = r.retrieve(history, seen, 11)
        assert result.content_personalized == 0
        assert result.content == result.collaborative
    assert r.retrieve([], [], 1).content == ()


def test_duplicate_history_events_are_not_reemitted(tmp_path):
    root, _, f = _fixture(tmp_path)
    r = load_r06_retrieval(root, f)
    # Repeated events contribute at their individual distances, but are excluded
    # from output, and cannot cause duplicate provider items.
    h = ["a"] * 4 + ["d"] * 4
    result = r.retrieve(h, set(h), 11)
    assert "a" not in result.content and "d" not in result.content
    assert len(result.content) == len(set(result.content))
    # 50 is an exact bound, not an undocumented truncation.
    assert r.retrieve(["ghost"] * 50, {"ghost"}, 11).content_personalized == 0


def test_exact_200_budget_id_ties_and_prior_pool_before_filtering(tmp_path):
    root, _, f = _fixture(tmp_path)
    r = load_r06_retrieval(root, f)
    ids = ("a",) + tuple(f"item{i:04}" for i in range(1299))
    metadata = tuple(replace(f._metadata[0], prior_strength=1.) for _ in ids)
    large = replace(f, item_ids=ids, _indices=MappingProxyType({item: i for i, item in enumerate(ids)}),
                    _metadata=metadata, _vectors=struct.pack("<2600f", *([1., 0.] * 1300)), _present=bytes([1])*1300)
    # Only a weak candidate outside the first 1000 has collaborative signal.
    offsets = (0,) + (1,)*1300
    priors = (1.,)*1000 + (.9,)*299 + (0.,)
    r = replace(r, _features=large, _ordered=tuple(range(1300)), _priors=priors,
                _offsets=offsets, _edges=struct.pack("<Id", 1299, .5))
    seen = set(ids[:1000])
    result = r.retrieve(["a"], seen, 11)
    assert len(result.collaborative) == len(set(result.collaborative)) == 200
    assert len(result.content) == len(set(result.content)) == 200
    assert result.content == ids[1000:1200]  # Exact equal scores use ID order.
    # Filtering the prior before slicing 1000 would wrongly put .75*.9 items
    # before the weak CF item (.25); original semantics put CF first then fill.
    assert result.collaborative[0] == ids[-1]
    assert result.collaborative[1:] == ids[1000:1199]


@pytest.mark.parametrize("history,seen,timestamp", [(["a"], [], 11), (["a"]*51, ["a"], 11),
    ("a", ["a"], 11), ([], "a", 11), ([], ["x"]*10001, 11), ([True], [True], 11),
    ([" "], [" "], 11), ([], [], True), ([], [], -1), ([], [], 253402300800000),
    (["e"], ["e"], 10), (["a"], ["a"], 1)])
def test_bad_request_is_rejected(tmp_path, history, seen, timestamp):
    root, _, f = _fixture(tmp_path)
    with pytest.raises(ControlledLoadError, match=".") as error:
        load_r06_retrieval(root, f).retrieve(history, seen, timestamp)
    assert error.value.code == "input_shape"


@pytest.mark.parametrize("key", [*BINDING_KEYS, "features_manifest_sha256", "selected_method"])
def test_every_source_binding_is_checked_before_replay(tmp_path, key):
    root, m, f = _fixture(tmp_path)
    m["provenance"][key] = "B-frozen-s17" if key == "selected_method" else "0" * len(m["provenance"][key])
    _save(root, m)
    with pytest.raises(ControlledLoadError) as error:
        load_r06_retrieval(root, f)
    assert error.value.code == "component_changed"


@pytest.mark.parametrize("key,value", [("edge_count", True), ("edge_count", -1), ("edge_count", 2000001),
    ("item_count", 7), ("schema_version", True), ("graph_dtype", "pickle"), ("kind", "tower"),
    ("extra", 1)])
def test_manifest_shape_and_resource_limits(tmp_path, key, value):
    root, m, f = _fixture(tmp_path)
    m[key] = value
    _save(root, m)
    with pytest.raises(ControlledLoadError):
        load_r06_retrieval(root, f)


@pytest.mark.parametrize("key,value", [("cf_alpha", .5), ("history_decay", 1.), ("provider_k", 100),
    ("prior_pool", True), ("content_arithmetic", "blas"), ("neighbors_per_item", 101)])
def test_protocol_is_closed_and_exact(tmp_path, key, value):
    root, m, f = _fixture(tmp_path)
    m["protocol"][key] = value
    _save(root, m)
    with pytest.raises(ControlledLoadError):
        load_r06_retrieval(root, f)


@pytest.mark.parametrize("value", [True, -1, 17, 10**500, float("nan"), float("inf")])
def test_prior_numbers_are_bounded(tmp_path, value):
    root, m, f = _fixture(tmp_path)
    counts = COUNTS.copy()
    counts[0] = value
    _change(root, m, "statistics", json.dumps(counts).encode())
    with pytest.raises(ControlledLoadError):
        load_r06_retrieval(root, f)


@pytest.mark.parametrize("field,value", [("end_ms", True), ("end_ms", 1), ("training_rows", 0),
    ("positive_rating_min", True), ("positive_rating_min", 3)])
def test_fit_protocol_and_availability_cannot_drift(tmp_path, field, value):
    root, m, f = _fixture(tmp_path)
    m["fit"][field] = value
    _save(root, m)
    with pytest.raises(ControlledLoadError):
        load_r06_retrieval(root, f)


@pytest.mark.parametrize("index,similarity", [(6, .5), (0, .5), (4, .5), (1, 0.), (1, -1.),
    (1, 1.1), (1, 5e-324), (1, float("nan")), (1, float("inf"))])
def test_invalid_neighbor_edges(tmp_path, index, similarity):
    root, m, f = _fixture(tmp_path)
    graph = bytearray((root / "neighbors.bin").read_bytes())
    struct.pack_into("<Id", graph, 28, index, similarity)
    _change(root, m, "neighbors", bytes(graph))
    with pytest.raises(ControlledLoadError):
        load_r06_retrieval(root, f)


@pytest.mark.parametrize("position,value", [(0, 1), (1, 101), (2, 1), (6, 6)])
def test_invalid_graph_offsets(tmp_path, position, value):
    root, m, f = _fixture(tmp_path)
    graph = bytearray((root / "neighbors.bin").read_bytes())
    struct.pack_into("<I", graph, position * 4, value)
    _change(root, m, "neighbors", bytes(graph))
    with pytest.raises(ControlledLoadError):
        load_r06_retrieval(root, f)


@pytest.mark.parametrize("mode", ["duplicate", "unsorted", "truncated", "trailing"])
def test_graph_rows_are_canonical(tmp_path, mode):
    root, m, f = _fixture(tmp_path)
    graph = bytearray((root / "neighbors.bin").read_bytes())
    if mode == "duplicate":
        struct.pack_into("<Id", graph, 40, 1, .25)
    elif mode == "unsorted":
        struct.pack_into("<Id", graph, 40, 2, .75)
    elif mode == "truncated":
        graph = graph[:-1]
    else:
        graph += b"x"
    _change(root, m, "neighbors", bytes(graph))
    with pytest.raises(ControlledLoadError):
        load_r06_retrieval(root, f)


@pytest.mark.parametrize("mode", ["swap", "duplicate", "only_cold", "only_represented", "empty", "extra"])
def test_full_provider_replay_cannot_be_weakened(tmp_path, mode):
    root, m, f = _fixture(tmp_path)
    samples = _samples()
    if mode == "swap":
        samples[0]["content"][0:2] = samples[0]["content"][0:2][::-1]
    elif mode == "duplicate":
        samples[0]["collaborative"].append("b")
    elif mode == "only_cold":
        samples = [samples[1], samples[1]]
    elif mode == "only_represented":
        samples = [samples[0], samples[0]]
    elif mode == "empty":
        samples = []
    else:
        samples[0]["target"] = "label"
    _change(root, m, "validation", json.dumps(samples).encode())
    with pytest.raises(ControlledLoadError):
        load_r06_retrieval(root, f)


def test_controlled_files_hashes_json_limits_and_immutable_state(tmp_path):
    root, m, f = _fixture(tmp_path)
    digest = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    r = load_r06_retrieval(root, f, expected_manifest_sha256=digest)
    with pytest.raises(TypeError):
        r.provenance["selected_method"] = "changed"
    with pytest.raises(AttributeError):
        r.edge_count = 0
    expected = r.retrieve(["a"], ["a"], 11)
    (root / "neighbors.bin").write_bytes(b"corrupted")
    assert r.retrieve(["a"], ["a"], 11) == expected
    with pytest.raises(ControlledLoadError) as error:
        load_r06_retrieval(root, f)
    assert error.value.code == "component_changed"
    with pytest.raises(ControlledLoadError):
        load_r06_retrieval(root, f, expected_manifest_sha256="0"*64)
    _change(root, m, "statistics", b"["*33 + b"0" + b"]"*33)
    with pytest.raises(ControlledLoadError) as error:
        load_r06_retrieval(root, f)
    assert error.value.code == "invalid_json"


@pytest.mark.parametrize("key", ["statistics", "neighbors", "validation"])
def test_path_escape_is_rejected(tmp_path, key):
    root, m, f = _fixture(tmp_path)
    m[key]["path"] = "../external"
    _save(root, m)
    with pytest.raises(ControlledLoadError) as error:
        load_r06_retrieval(root, f)
    assert error.value.code == "unsafe_path"


def test_service_imports_and_replay_require_no_research_packages(tmp_path):
    root, _, _ = _fixture(tmp_path)
    code = '''import importlib.abc, sys
class Deny(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0] in {'torch','numpy','scipy','sklearn','joblib','pickle'}: raise AssertionError(fullname)
sys.meta_path.insert(0,Deny())
from evorec.infrastructure.r06_features import load_r06_features
from evorec.infrastructure.r06_retrieval import load_r06_retrieval
r=load_r06_retrieval(sys.argv[1],load_r06_features(sys.argv[2]))
assert r.retrieve(['a'],['a'],11).content[0]=='e'
'''
    result = subprocess.run([sys.executable, "-c", code, str(root), str(tmp_path / "features")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_zero_edges_and_file_resource_limit(tmp_path, monkeypatch):
    root, m, f = _fixture(tmp_path)
    m["edge_count"] = 0
    _change(root, m, "neighbors", struct.pack("<7I", *([0]*7)))
    samples = _samples()
    samples[0]["collaborative"] = ["zero", "b", "c", "d"]
    _change(root, m, "validation", json.dumps(samples).encode())
    assert load_r06_retrieval(root, f).edge_count == 0
    monkeypatch.setattr(module, "MAX_JSON_BYTES", 3)
    with pytest.raises(ControlledLoadError) as error:
        load_r06_retrieval(root, f)
    assert error.value.code == "resource_limit"


def test_cli_requires_pinned_components_and_never_activates(tmp_path, capsys):
    from scripts.load_r06_retrieval import main
    root, _, f = _fixture(tmp_path)
    digest = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    args = [str(root), "--expected-manifest-sha256", digest, "--features-component", str(tmp_path / "features"),
            "--expected-features-manifest-sha256", f.manifest_sha256]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["component_only"] and not result["activated"] and result["validation_samples_checked"] == 2
    assert not result["ranker_binding_checked"]
    with pytest.raises(SystemExit) as error:
        main(args + ["--ranker-component", "unused"])
    assert error.value.code == 2
    args[2] = "0"*64
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "component_changed"
