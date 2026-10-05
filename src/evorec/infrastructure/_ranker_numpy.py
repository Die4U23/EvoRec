"""Explicit, bounded float64 MLP execution matching the scalar loader policy.

No training, file reads, Torch/SciPy, global NumPy settings or silent fallback.
Final float32 scores and original references/order are checked by the loader.
"""

from dataclasses import dataclass
import math

from evorec.infrastructure.residual_ranker import ControlledLoadError, _f32, _gelu


@dataclass(frozen=True)
class NumpyMLP:
    np: object
    layers: tuple
    base_scale: float
    residual_scale: float

    def score(self, context, candidates, scalars, *, empty):
        np = self.np
        try:
            with np.errstate(over="raise", invalid="raise", under="ignore"):
                rows = np.asarray(candidates, dtype=np.float64)
                fields = np.asarray(scalars, dtype=np.float64)
                base = self.base_scale * .5 * (fields[:, 1] + fields[:, 2])
                if empty:
                    return tuple(_f32(float(value)) for value in base)
                history = np.broadcast_to(np.asarray(context, dtype=np.float64), rows.shape)
                values = np.concatenate((history, rows, history * rows, np.abs(history - rows), fields), axis=1)
                for weights, biases in self.layers[:-1]:
                    values = values @ weights.T + biases
                    # Scalar erf/GELU preserves the existing policy; no SciPy approximation.
                    values = np.fromiter((_gelu(float(v)) for v in values.flat), dtype=np.float64,
                                         count=values.size).reshape(values.shape)
                weights, biases = self.layers[-1]
                delta = values @ weights.T + biases
                result = tuple(_f32(float(b) + self.residual_scale * math.tanh(float(d)))
                               for b, d in zip(base, delta[:, 0], strict=True))
                if not all(math.isfinite(value) for value in result):
                    raise FloatingPointError("non-finite final score")
                return result
        except (FloatingPointError, OverflowError) as error:
            raise ControlledLoadError("backend_arithmetic", "NumPy residual scoring failed") from error


def numpy_mlp(runtime):
    try:
        import numpy as np
    except ImportError as error:
        raise ControlledLoadError("backend_unavailable", "explicit NumPy ranker requires the optional dependency") from error
    if np.__version__ != "2.1.3":
        raise ControlledLoadError("unsupported_backend", "NumPy ranker requires audited version 2.1.3")
    layers = []
    for weights, biases in runtime._layers:
        matrix = np.asarray(weights, dtype="<f8")
        # Views backed by immutable bytes cannot be made writeable by a caller.
        matrix = np.frombuffer(matrix.tobytes(), dtype="<f8").reshape(matrix.shape)
        bias = np.frombuffer(np.asarray(biases, dtype="<f8").tobytes(), dtype="<f8")
        layers.append((matrix, bias))
    return NumpyMLP(np, tuple(layers), runtime.base_scale, runtime.residual_scale)
