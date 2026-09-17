"""Paired user-cluster bootstrap preserving the request-weighted estimand."""
import numpy as np
from threadpoolctl import threadpool_limits


def clustered_mean_interval(differences, users, *, replicates=10000, seed=20260916,
                            confidence=0.95, batch_size=64):
    """Resample complete users jointly across all paired metric columns."""
    values = np.asarray(differences, dtype=np.float64)
    users = np.asarray(users)
    if values.ndim == 1:
        values = values[:, None]
    if (values.ndim != 2 or values.shape[1] == 0 or not len(values)
            or users.ndim != 1 or len(users) != len(values)
            or not np.isfinite(values).all()):
        raise ValueError("expected finite request differences and matching user IDs")
    if (type(replicates) is not int or replicates < 2
            or type(batch_size) is not int or batch_size < 1
            or not 0 < confidence < 1):
        raise ValueError("invalid bootstrap settings")
    labels, inverse = np.unique(users, return_inverse=True)
    count = len(labels)
    if count < 2:
        raise ValueError("at least two independent user clusters are required")
    sizes = np.bincount(inverse).astype(np.float64)
    sums = np.zeros((count, values.shape[1]), dtype=np.float64)
    np.add.at(sums, inverse, values)
    rng = np.random.default_rng(seed)
    samples = np.empty((replicates, values.shape[1]), dtype=np.float64)
    with threadpool_limits(limits=4):
        for start in range(0, replicates, batch_size):
            size = min(batch_size, replicates - start)
            # Multinomial counts equal drawing count users with replacement.
            weights = rng.multinomial(count, np.full(count, 1 / count), size=size)
            samples[start:start + size] = (weights @ sums) / (weights @ sizes)[:, None]
    tail = (1 - confidence) / 2
    bounds = np.quantile(samples, [tail, 1 - tail], axis=0, method="linear")
    return {
        "estimate": values.mean(axis=0).tolist(),
        "low": bounds[0].tolist(),
        "high": bounds[1].tolist(),
        "requests": len(values),
        "users": count,
        "replicates": replicates,
        "seed": seed,
        "confidence": confidence,
        "unit": "user",
        "estimator": "request-weighted mean paired difference",
        "interval": "percentile",
    }
