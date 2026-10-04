"""Opt-in kernel precision, bounded memory, thread safety and strict replay."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import heapq
import json
import struct

import pytest

np = pytest.importorskip("numpy")

from evorec.infrastructure import _content_numpy as kernel
from evorec.infrastructure.r06_retrieval import _content_score, load_r06_retrieval
from evorec.infrastructure.residual_ranker import ControlledLoadError
from test_r06_retrieval_runtime import _fixture, _change, _samples


@pytest.mark.parametrize("dimension", [1, 2, 3, 128])
def test_randomized_scores_are_bitwise_scalar_equivalent(dimension):
    random = np.random.default_rng(1706 + dimension)
    vectors = random.uniform(-1., 1., (200, dimension)).astype(np.float32)
    context = random.uniform(-1., 1., dimension).astype(np.float32)
    actual = kernel._scores(np, vectors, context)
    expected = [_content_score(tuple(float(v) for v in context), tuple(float(v) for v in row)) for row in vectors]
    assert actual.astype("<f4").tobytes() == struct.pack(f"<{len(expected)}f", *expected)


def test_analytical_rounding_signed_zero_and_subnormal_cases():
    rows = np.array([[1., 2**-25, -1.], [-0., -0., -0.], [2**-149, 0., 0.]], dtype=np.float32)
    expected = struct.pack("<3f", 0., 0., 2**-149)
    assert kernel._scores(np, rows, np.ones(3, dtype=np.float32)).astype("<f4").tobytes() == expected
    assert kernel._scores(np, rows[-1:], np.array([.5, 1., 1.], dtype=np.float32))[0] == 0.
    # np.sum's alternative reduction and a final float32 cast are not substitutes.
    assert sum((1., 2**-25, -1.)) != 0.


def test_full_replay_and_concurrent_requests_preserve_bytes_and_host_error_policy(tmp_path):
    root, _, features = _fixture(tmp_path)
    digest = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    scalar = load_r06_retrieval(root, features, expected_manifest_sha256=digest)
    original_vectors = features._vectors
    previous = np.geterr()
    with np.errstate(under="raise", invalid="ignore"):
        policy = np.geterr()
        accelerated = load_r06_retrieval(root, features, expected_manifest_sha256=digest, content_backend="numpy")
        assert np.geterr() == policy
        requests = [(["a"], {"a", "b"}, 11), ([], [], 11), (["zero"], ["zero"], 11),
                    (["a", "ghost"], ["a", "ghost"], 11), (["a"]*50, ["a"], 11)]
        expected = [scalar.retrieve(*values) for values in requests]
        with ThreadPoolExecutor(max_workers=4) as pool:
            actual = list(pool.map(lambda i: accelerated.retrieve(*requests[i % len(requests)]), range(30)))
        assert actual == [expected[i % len(requests)] for i in range(30)]
        assert accelerated.content_backend == "numpy" and scalar.content_backend == "stdlib"
        assert features._vectors is original_vectors
        assert np.geterr() == policy
    assert np.geterr() == previous


@pytest.mark.parametrize("dimension", [2, 128])
def test_chunk_boundary_filters_ties_and_readonly_feature_views(tmp_path, monkeypatch, dimension):
    _, _, f = _fixture(tmp_path)
    count = kernel.BLOCK_ITEMS + 1
    ids = tuple(f"item{i:05}" for i in range(count))
    metadata = [f._metadata[0]]*count
    metadata[100] = replace(metadata[100], first_seen_ms=11)
    present = bytearray([1]*count)
    present[13] = 0
    vector = (1.,) + (0.,)*(dimension-1)
    vectors = struct.pack(f"<{count*dimension}f", *(vector*count))
    f = replace(f, dimension=dimension, item_ids=ids, _metadata=tuple(metadata),
                _vectors=vectors, _present=bytes(present))
    scanner = kernel.numpy_scanner()
    original = kernel._scores
    blocks = []
    def checked(np_arg, matrix, context):
        assert not matrix.flags.writeable
        with pytest.raises(ValueError):
            matrix[0, 0] = 0.
        assert len(matrix) <= kernel.BLOCK_ITEMS
        blocks.append(matrix.shape)
        return original(np_arg, matrix, context)
    monkeypatch.setattr(kernel, "_scores", checked)
    result = heapq.nsmallest(200, scanner(f, vector, {ids[1]}, 11))
    assert blocks == [(4096, dimension), (1, dimension)]
    assert [index for _, index in result] == [i for i in range(count) if i not in (1, 13, 100)][:200]
    assert f._vectors == vectors


def test_version_and_arithmetic_drift_fail_closed(tmp_path, monkeypatch):
    root, _, f = _fixture(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(np, "__version__", "999.0")
        with pytest.raises(ControlledLoadError) as error:
            load_r06_retrieval(root, f, content_backend="numpy")
        assert error.value.code == "unsupported_backend"
    original = kernel._scores
    monkeypatch.setattr(kernel, "_scores", lambda np_arg, v, c: original(np_arg, v, c) + np.float32(.001))
    with pytest.raises(ControlledLoadError) as error:
        load_r06_retrieval(root, f, content_backend="numpy")
    assert error.value.code == "backend_arithmetic"


def test_source_binding_and_full_order_validation_are_not_bypassed(tmp_path, monkeypatch):
    root, m, f = _fixture(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(kernel, "numpy_scanner", lambda: pytest.fail("source must be checked before the backend"))
        with pytest.raises(ControlledLoadError) as error:
            load_r06_retrieval(root, f, expected_manifest_sha256="0"*64, content_backend="numpy")
        assert error.value.code == "component_changed"
    samples = _samples()
    samples[0]["content"][0:2] = samples[0]["content"][0:2][::-1]
    _change(root, m, "validation", json.dumps(samples).encode())
    with pytest.raises(ControlledLoadError) as error:
        load_r06_retrieval(root, f, content_backend="numpy")
    assert error.value.code == "validation_mismatch"


def test_cli_explicit_backend_reports_real_execution(tmp_path, capsys):
    from scripts.load_r06_retrieval import main
    root, _, f = _fixture(tmp_path)
    digest = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    assert main([str(root), "--expected-manifest-sha256", digest, "--features-component", str(tmp_path / "features"),
                 "--expected-features-manifest-sha256", f.manifest_sha256, "--content-backend", "numpy"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["content_backend"] == "numpy" and not result["activated"]


def test_live_subset_parity_including_large_budget_refill(tmp_path):
    from test_r06_serving import _large_fixture
    root, features = _large_fixture(tmp_path)
    scalar = load_r06_retrieval(root, features)
    accelerated = load_r06_retrieval(root, features, content_backend="numpy")
    for eligible in (frozenset(features.item_ids[400:]), frozenset(), frozenset(features.item_ids[::3])):
        actual = accelerated.retrieve([features.item_ids[0]], {features.item_ids[0]}, 11, eligible_items=eligible)
        expected = scalar.retrieve([features.item_ids[0]], {features.item_ids[0]}, 11, eligible_items=eligible)
        assert actual == expected


@pytest.mark.parametrize("history", [("a",), (), ("unknown", "a"), ("zero",)])
def test_numpy_full_package_matches_pure_wrapper_with_actual_catalog(tmp_path, history):
    from test_r06_bundle import _build, _records
    from test_r06_serving import _request
    from evorec.infrastructure.r06_bundle import load_r06_bundle
    root, target, digest = _build(tmp_path)
    pure = load_r06_bundle(root, target, expected_manifest_sha256=digest)
    fast = load_r06_bundle(root, target, expected_manifest_sha256=digest, content_backend="numpy")
    request = _request(pure.adapter, history=history, eligible={"b", "c", "e", "zero"})
    rows = _records(pure, request.context.catalog.eligible_items)
    assert pure.score(request.context, 11, request.full_seen, rows) == fast.score(request.context, 11, request.full_seen, rows)
