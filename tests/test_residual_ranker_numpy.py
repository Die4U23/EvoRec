"""Optional bounded residual MLP numerics and complete controlled bundle parity."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import math
import struct

import pytest

np = pytest.importorskip("numpy")

from evorec.infrastructure._ranker_numpy import NumpyMLP
from evorec.infrastructure.residual_ranker import (
    ControlledLoadError, ResidualRanker, SCORE_TOLERANCE, load_residual_ranker,
)
from test_residual_ranker_runtime import _fixture, _reference
from test_r06_bundle import _build, _records
from test_r06_serving import _request


def test_independent_analytic_scores_and_readonly_owned_layers(tmp_path):
    root, _, samples = _fixture(tmp_path)
    runtime = load_residual_ranker(root, cpu_backend="numpy")
    for sample in samples:
        expected = tuple(_reference(sample["context"][0], row[0], fields)
                         for row,fields in zip(sample["candidates"], sample["scalars"], strict=True))
        assert runtime.score(sample["context"], sample["candidates"], sample["scalars"]) == expected
    for weights,biases in runtime._accelerated.layers:
        for array in (weights, biases):
            with pytest.raises(ValueError): array.setflags(write=True)
    (root / "weights.f32").write_bytes(b"changed original")
    assert runtime.score(samples[0]["context"], samples[0]["candidates"], samples[0]["scalars"]) == tuple(samples[0]["expected_scores"])


@pytest.mark.parametrize("dimension,hidden,bottleneck", [(1, 2, 1), (3, 7, 4), (128, 128, 64)])
def test_randomized_400_candidate_parity_preserves_order_and_host_policy(dimension, hidden, bottleneck):
    from evorec.infrastructure._ranker_numpy import numpy_mlp
    from types import MappingProxyType
    random = np.random.default_rng(170610 + dimension)
    shapes = ((4*dimension+8, hidden), (hidden, bottleneck), (bottleneck, 1))
    layers = tuple((tuple(tuple(float(v) for v in row) for row in random.normal(0, .01, (o,i)).astype(np.float32)),
                    tuple(float(v) for v in random.normal(0, .01, o).astype(np.float32))) for i,o in shapes)
    scalar = ResidualRanker(dimension, hidden, bottleneck, 8., 4., "a"*64, MappingProxyType({}), 0, layers)
    accelerated = replace(scalar, _accelerated=numpy_mlp(scalar))
    context = tuple(float(v) for v in random.uniform(-1, 1, dimension).astype(np.float32))
    rows = [list(float(v) for v in row) for row in random.uniform(-1, 1, (400, dimension)).astype(np.float32)]
    fields = [list(float(v) for v in row) for row in random.uniform(0, 1, (400, 8)).astype(np.float32)]
    previous = np.geterr()
    with np.errstate(under="raise", invalid="ignore"):
        policy = np.geterr()
        actual = accelerated.score(context, rows, fields)
        assert np.geterr() == policy
    assert np.geterr() == previous
    expected = scalar.score(context, rows, fields)
    assert max(abs(a-b) for a,b in zip(actual, expected, strict=True)) <= SCORE_TOLERANCE
    assert sorted(range(400), key=lambda i: (-actual[i],i)) == sorted(range(400), key=lambda i: (-expected[i],i))
    empty = accelerated.score((0.,)*dimension, rows, fields)
    assert empty == scalar.score((0.,)*dimension, rows, fields)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: accelerated.score(context, rows, fields), range(4))) == [actual]*4


@pytest.mark.parametrize("context,rows,fields", [
    ([], [[1.]], [[0.]*8]), ([True], [[1.]], [[0.]*8]), ([math.nan], [[1.]], [[0.]*8]),
    ([1.], [], []), ([1.], [[1.]]*401, [[0.]*8]*401), ([1.], [[1., 2.]], [[0.]*8]),
    ([1.], [[1.]], [[0.]*7]), ([1.], [[math.inf]], [[0.]*8]),
])
def test_accelerated_input_guards_run_before_vectorized_math(tmp_path, context, rows, fields):
    root, _, _ = _fixture(tmp_path)
    runtime = load_residual_ranker(root, cpu_backend="numpy")
    with pytest.raises(ControlledLoadError): runtime.score(context, rows, fields)


def test_explicit_version_dependency_and_backend_validation(tmp_path, monkeypatch):
    root, _, _ = _fixture(tmp_path)
    with pytest.raises(ControlledLoadError) as error: load_residual_ranker(root, cpu_backend="auto")
    assert error.value.code == "unsupported_backend"
    with monkeypatch.context() as patch:
        patch.setattr(np, "__version__", "0.0")
        with pytest.raises(ControlledLoadError) as error: load_residual_ranker(root, cpu_backend="numpy")
        assert error.value.code == "unsupported_backend"
    import builtins
    original = builtins.__import__
    def reject(name, *args, **kwargs):
        if name == "numpy": raise ImportError("synthetic missing NumPy")
        return original(name, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", reject)
        with pytest.raises(ControlledLoadError) as error: load_residual_ranker(root, cpu_backend="numpy")
        assert error.value.code == "backend_unavailable"
        assert load_residual_ranker(root)._accelerated is None


def test_actual_accelerated_golden_fault_cannot_return_runtime(tmp_path, monkeypatch):
    root, _, _ = _fixture(tmp_path)
    monkeypatch.setattr(NumpyMLP, "score", lambda self, c, rows, s, **kw: (99.,)*len(rows))
    with pytest.raises(ControlledLoadError) as error: load_residual_ranker(root, cpu_backend="numpy")
    assert error.value.code == "validation_mismatch"


def test_full_controlled_bundle_double_acceleration_keeps_model_and_scores(tmp_path):
    from evorec.infrastructure.r06_bundle import load_r06_bundle
    root,target,digest = _build(tmp_path)
    scalar = load_r06_bundle(root,target,expected_manifest_sha256=digest)
    accelerated = load_r06_bundle(root,target,expected_manifest_sha256=digest,
                                  content_backend="numpy", ranker_backend="numpy")
    request = _request(scalar.adapter, eligible={"c", "d", "e", "zero"})
    rows = _records(scalar, request.context.catalog.eligible_items)
    left = scalar.score(request.context, 11, request.full_seen, rows)
    right = accelerated.score(request.context, 11, request.full_seen, rows)
    assert left == right and scalar.model_version == accelerated.model_version


def test_backend_arithmetic_failure_is_not_silently_scalar_fallback(tmp_path):
    root, _, _ = _fixture(tmp_path)
    runtime = load_residual_ranker(root, cpu_backend="numpy")
    class FailedHost:
        def errstate(self, **kwargs): raise FloatingPointError("injected host arithmetic failure")
    runtime = replace(runtime, _accelerated=replace(runtime._accelerated, np=FailedHost()))
    with pytest.raises(ControlledLoadError) as error:
        runtime.score([1.], [[1.]], [[0.]*8])
    assert error.value.code == "backend_arithmetic"
