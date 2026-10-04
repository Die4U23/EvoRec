"""Opt-in bounded CPU acceleration; no dot/BLAS reduction or model file reads.

NumPy accumulate's declared intermediate dtype/order is essential, not sum/dot:
https://numpy.org/doc/2.1/reference/generated/numpy.ufunc.accumulate.html
"""

import struct

from evorec.infrastructure.residual_ranker import ControlledLoadError, _f32

NUMPY_VERSION = "2.1.3"
BLOCK_ITEMS = 4096


def _scores(np, vectors, context):
    # Separate multiply and accumulation prohibit a fused multiply-add. The
    # first add also reproduces the scalar accumulator's positive-zero identity.
    with np.errstate(over="raise", invalid="raise", under="ignore"):
        product = np.multiply(vectors, context, dtype=np.float32)
        np.add(product[:, 0], np.float32(0.), out=product[:, 0])
        np.add.accumulate(product, axis=1, dtype=np.float32, out=product)
    return product[:, -1]


def numpy_scanner():
    try:
        import numpy as np
    except ImportError as error:
        raise ControlledLoadError("backend_unavailable", "explicit NumPy backend requires the optional dependency") from error
    if np.__version__ != NUMPY_VERSION:
        raise ControlledLoadError("unsupported_backend", "NumPy backend requires the audited version 2.1.3")
    # Runtime arithmetic gate, including subnormals, signed zero and rounding.
    contexts = ((1., 1., 1.), (.6, .8, -1.), (2**-126, 1., 1.))
    rows = ((1., 2**-25, -1.), (-0., -0., -0.), (2**-149, 1., -1.), (.6, .8, 2**-25))
    matrix = np.asarray(rows, dtype=np.float32)
    try:
        for context in contexts:
            context = tuple(_f32(v) for v in context)
            actual = _scores(np, matrix, np.asarray(context, dtype=np.float32))
            for row, value in zip(rows, actual, strict=True):
                expected = 0.
                for a, b in zip(context, row, strict=True):
                    expected = _f32(expected + _f32(a * _f32(b)))
                if struct.pack("<f", float(value)) != struct.pack("<f", expected):
                    raise ControlledLoadError("backend_arithmetic", "NumPy CPU arithmetic does not match the frozen protocol")
    except FloatingPointError as error:
        raise ControlledLoadError("backend_arithmetic", "NumPy arithmetic self-check failed") from error

    def scan(features, context, seen, timestamp_ms, *, eligible_items=None):
        # Only ephemeral views of immutable bytes; no persistent mutable ndarray.
        vectors = np.frombuffer(features._vectors, dtype="<f4").reshape(-1, features.dimension)
        context = np.asarray(context, dtype=np.float32)
        try:
            for start in range(0, len(features.item_ids), BLOCK_ITEMS):
                stop = min(start + BLOCK_ITEMS, len(features.item_ids))
                eligible = [i for i in range(start, stop) if features._present[i]
                            and features._metadata[i].first_seen_ms < timestamp_ms
                            and features.item_ids[i] not in seen
                            and (eligible_items is None or features.item_ids[i] in eligible_items)]
                if not eligible:
                    continue
                scores = _scores(np, vectors[start:stop], context)
                for index in eligible:
                    yield -float(scores[index - start]), index
                # Release the block before allocating the next product matrix.
                del scores
        except FloatingPointError as error:
            raise ControlledLoadError("backend_arithmetic", "NumPy content scoring failed") from error

    return scan
