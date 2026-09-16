from types import SimpleNamespace
import pytest
torch = pytest.importorskip("torch")
pytest.importorskip("sklearn")
import numpy as np
from evorec.research.baselines import Ranking
from evorec.research.content import ContentFeatures, ContentPredictor, ContentTower, reciprocal_fusion
from evorec.research.neural import configure_seed
from evorec.research.protocol import Query
from evorec.research.training_baselines import RecentPopular


def test_text_fit_excludes_catalog_only_words_and_roundtrips(tmp_path):
    protocol = SimpleNamespace(
        config={"content": {"max_features": 100, "min_df": 1, "dimensions": 2, "svd_seed": 17}},
        vocabulary=("a", "b", "c"), catalog=dict.fromkeys(("a", "b", "c", "new", "empty"), 1),
        metadata={"a": "racing game console", "b": "racing action console", "c": "puzzle game console",
                  "new": "racing futureexclusiveword", "empty": ""}, protocol_id="toy",
    )
    features, manifest = ContentFeatures.fit(protocol, tmp_path / "encoder")
    import joblib
    payload = joblib.load(tmp_path / "encoder/encoder.joblib")
    assert "futureexclusiveword" not in payload["vectorizer"].vocabulary_
    assert features.present[features.mapping["new"]]
    assert not features.present[features.mapping["empty"]]
    assert manifest["fit_document_count"] == 3 and manifest["encoder_reload_verified"]


def test_history_decay_and_missing_history():
    features = ContentFeatures(("a", "b"), [[1, 0], [0, 1]])
    matrix = features.histories([("a", "unknown", "b"), ("unknown",)], decay=.5)
    expected = np.array([.25, 1]) / np.linalg.norm([.25, 1])
    np.testing.assert_allclose(matrix[0], expected, atol=1e-7)
    assert not matrix[1].any()


def test_retrieve_cold_item_and_filter_time_seen_missing():
    features = ContentFeatures(("seen", "cold", "future", "missing", "other"),
                               [[1, 0], [1, 0], [1, 0], [0, 0], [0, 1]])
    catalog = {"seen": 1, "cold": 2, "future": 10, "missing": 1, "other": 1}
    prior = RecentPopular().fit([SimpleNamespace(item_id="other", timestamp_ms=1, rating=5)], cutoff=5)
    predictor = ContentPredictor(features, catalog, prior, device="cpu", batch_size=1)
    query = Query("q", 10, "cold", ("seen",), frozenset({"seen"}), True, True)
    ranking = predictor.rank_many([query], k=2)[0]
    assert ranking.items == ("cold", "other")
    assert "future" not in ranking.items and "missing" not in ranking.items
    empty = Query("empty", 10, "other", ("unknown",), frozenset(), True, False)
    fallback = predictor.rank_many([empty])[0]
    assert fallback.items == ("other",) and fallback.personalized_count == 0


def test_tower_starts_as_cosine_and_learns():
    configure_seed(17)
    values = torch.eye(4)
    model = ContentTower(4, hidden=8)
    torch.testing.assert_close(model.user_vectors(values), values)
    targets = torch.tensor([1, 2, 3, 0])
    optimizer = torch.optim.AdamW(model.parameters(), lr=.03)
    initial = torch.nn.functional.cross_entropy(model(values, values), targets).item()
    for _ in range(60):
        optimizer.zero_grad()
        loss = torch.nn.functional.cross_entropy(model(values, values), targets)
        loss.backward()
        optimizer.step()
    assert loss.item() < initial * .5
    assert torch.isfinite(model.item_vectors(values)).all()


def test_rank_fusion_endpoints_and_deduplication():
    left, right = [Ranking(("a", "b"))], [Ranking(("c", "b"))]
    assert reciprocal_fusion(left, right, alpha=0)[0].items == ("a", "b")
    assert reciprocal_fusion(left, right, alpha=1)[0].items == ("c", "b")
    fused = reciprocal_fusion(left, right, alpha=.5)[0].items
    assert fused[0] == "b" and len(fused) == len(set(fused)) == 3
    with pytest.raises(ValueError):
        reciprocal_fusion(left, [], alpha=.25)


def test_checkpoint_roundtrip_binds_feature_values_and_protocol(tmp_path):
    from evorec.research.train_content import load_tower
    features = ContentFeatures(("a", "b"), [[1, 0], [0, 1]])
    model = ContentTower(2, 4)
    path = tmp_path / "best.pt"
    torch.save({"protocol_id": "p", "feature_shape": [2, 2],
                "feature_fingerprint": features.fingerprint, "setting": {"hidden": 4},
                "state_dict": model.state_dict()}, path)
    restored = load_tower(path, SimpleNamespace(protocol_id="p"), features, "cpu")
    values = torch.eye(2)
    torch.testing.assert_close(restored(values, values), model(values, values))
    with pytest.raises(ValueError, match="protocol mismatch"):
        load_tower(path, SimpleNamespace(protocol_id="different"), features, "cpu")
    changed = ContentFeatures(("a", "b"), [[0, 1], [1, 0]])
    with pytest.raises(ValueError, match="protocol mismatch"):
        load_tower(path, SimpleNamespace(protocol_id="p"), changed, "cpu")
