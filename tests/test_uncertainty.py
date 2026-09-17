import pytest
np = pytest.importorskip("numpy")

from evorec.research.uncertainty import clustered_mean_interval


def test_unequal_cluster_sizes_keep_request_weighting():
    values = np.array([1.] * 9 + [0.])
    users = np.array(["a"] * 9 + ["b"])
    result = clustered_mean_interval(values, users, replicates=500, seed=19)
    assert result["estimate"] == [0.9]
    assert result["low"] == [0.] and result["high"] == [1.]
    # Independently enumerate the actual draws; averaging user means would fail.
    weights = np.random.default_rng(19).multinomial(2, [0.5, 0.5], size=500)
    samples = weights[:, 0] * 9 / (weights[:, 0] * 9 + weights[:, 1])
    expected = np.quantile(samples, [0.025, 0.975])
    np.testing.assert_array_equal([result["low"][0], result["high"][0]], expected)


def test_columns_are_paired_and_zero_difference_is_exact():
    x = np.array([0., 0.2, 0.5, 1., -0.1])
    result = clustered_mean_interval(np.column_stack((x, x, x * 0)),
                                     ["a", "a", "b", "c", "c"], replicates=200)
    assert result["low"][0] == result["low"][1]
    assert result["high"][0] == result["high"][1]
    assert result["estimate"][2] == result["low"][2] == result["high"][2] == 0


def test_batch_size_does_not_change_draws():
    args = (np.array([1., 0., 0.2, 0.7]), ["a", "b", "b", "c"])
    a = clustered_mean_interval(*args, replicates=203, seed=8, batch_size=17)
    b = clustered_mean_interval(*args, replicates=203, seed=8, batch_size=64)
    assert a == b


@pytest.mark.parametrize("values,users", [
    ([], []), ([1.], ["only"]), ([1., float("nan")], ["a", "b"]),
    ([1., 2.], ["a"]), ([[1.], [2.]], [["a"], ["b"]]),
])
def test_invalid_or_single_cluster_input_is_rejected(values, users):
    with pytest.raises(ValueError):
        clustered_mean_interval(values, users)


@pytest.mark.parametrize("settings", [
    {"replicates": 1}, {"replicates": 2.5}, {"confidence": 1}, {"batch_size": 0}
])
def test_invalid_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        clustered_mean_interval([0., 1.], ["a", "b"], **settings)
