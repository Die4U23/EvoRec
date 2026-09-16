from types import SimpleNamespace

import pytest

torch = pytest.importorskip("torch")

from evorec.research.baselines import Popular
from evorec.research.data import Event
from evorec.research.neural import NeuralPredictor, SequenceModel, configure_seed
from evorec.research.protocol import Query
from evorec.research.train import load_model


def small_model():
    configure_seed(17)
    return SequenceModel(3, 5, hidden=8, heads=2, layers=1, dropout=0)


def test_causal_encoder_output_ignores_future_and_padded_tokens():
    model = small_model().eval()
    sequences = torch.tensor([[1, 2, 0, 0, 0], [1, 2, 3, 3, 3]])
    with torch.no_grad():
        outputs = model(sequences, torch.tensor([2, 2]))
    assert torch.allclose(outputs[0], outputs[1], atol=1e-6)


def test_empty_history_is_not_silently_sent_into_attention():
    with pytest.raises(ValueError, match="empty"):
        small_model()(torch.zeros((1, 5), dtype=torch.long), torch.tensor([0]))


def test_full_softmax_training_reduces_toy_loss():
    model = small_model()
    sequences = torch.tensor([[1, 0, 0, 0, 0], [2, 0, 0, 0, 0]])
    lengths = torch.tensor([1, 1])
    targets = torch.tensor([1, 2])
    optimizer = torch.optim.AdamW(model.parameters(), lr=.03)
    initial = torch.nn.functional.cross_entropy(model(sequences, lengths), targets).item()
    for _ in range(20):
        optimizer.zero_grad()
        loss = torch.nn.functional.cross_entropy(model(sequences, lengths), targets)
        loss.backward()
        optimizer.step()
    final = torch.nn.functional.cross_entropy(model(sequences, lengths), targets).item()
    assert final < initial * .5


def test_batched_prediction_filters_seen_future_items_and_handles_cold_history():
    model = small_model()
    popular = Popular().fit([Event("u", "a", 5, 1), Event("v", "b", 5, 1)])
    predictor = NeuralPredictor(model, ("a", "b", "c"), popular, {"a": 1, "b": 1, "c": 200}, device="cpu")
    queries = [
        Query("q1", 100, "b", ("a",), frozenset({"a"}), True, False),
        Query("q2", 100, "b", (), frozenset(), True, False),
    ]
    results = predictor.rank_many(queries)
    assert results[0].items == ("b",)
    assert results[1].items == ("a", "b")
    assert results[1].popularity_fill == 2


def test_checkpoint_reload_is_exact_and_rejects_wrong_protocol(tmp_path):
    model = small_model().eval()
    path = tmp_path / "model.pt"
    torch.save({
        "protocol_id": "test-protocol", "vocabulary": ("a", "b", "c"),
        "architecture": {"hidden": 8, "heads": 2, "layers": 1, "dropout": 0},
        "state_dict": model.state_dict(),
    }, path)
    protocol = SimpleNamespace(protocol_id="test-protocol", vocabulary=("a", "b", "c"), config={"history_limit": 5})
    loaded = load_model(path, protocol, "cpu").eval()
    sequences, lengths = torch.tensor([[1, 2, 0, 0, 0]]), torch.tensor([2])
    assert torch.equal(model(sequences, lengths), loaded(sequences, lengths))
    protocol.protocol_id = "wrong"
    with pytest.raises(ValueError, match="protocol"):
        load_model(path, protocol, "cpu")
