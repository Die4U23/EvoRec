"""Genuine research oracles, strict temporal reconstruction and fail-closed export."""

import csv
import json
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("sklearn")
import numpy as np

from evorec.infrastructure.r06_features import PROVENANCE_HASHES
from evorec.infrastructure.r06_retrieval import load_r06_retrieval
from evorec.infrastructure.residual_ranker import ControlledLoadError, PROVENANCE_HASHES as RANKER_HASHES
from evorec.research import export_retrieval as module
from evorec.research.baselines import ItemCF, Ranking
from evorec.research.content import ContentFeatures, ContentPredictor
from evorec.research.data import Event, load_events
from evorec.research.export_features import write_features
from evorec.research.export_ranker import _sha, validation_samples, write_component
from evorec.research.protocol import AvailableAt
from evorec.research.ranker import ResidualListRanker, build_pool_inputs
from evorec.research.training_baselines import CollaborativeBlend, RecentPopular


class LabelTrap:
    def __init__(self, history, seen, timestamp):
        self.history, self.seen, self.timestamp_ms = history, frozenset(seen), timestamp

    @property
    def target(self):
        pytest.fail("retrieval must not access target labels")

    target_model_cold = target
    target_available = target


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _project(tmp_path):
    start, cutoff = 1262304000000, 1262390400000
    training_path = tmp_path / "datasets/video_games_r03.csv"
    training_path.parent.mkdir()
    rows = [("u1", "a", 5, start), ("u1", "b", 4, start+1), ("u1", "c", 5, start+2),
            ("u2", "a", 5, start), ("u2", "b", 5, start+1), ("u2", "d", 4, start+3),
            ("u3", "a", 4, start), ("u3", "zero", 5, start+1),
            ("u4", "zero", 4, start), ("u5", "zero", 5, start),
            ("u6", "e", 5, cutoff),  # equality must not enter training
            ("u1", "e", 1, start),  # low rating seen/training item, not a positive CF edge
            ("u7", "a", 5, cutoff+1)]  # future positive cannot change the frozen prior
    with training_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("user_id", "parent_asin", "rating", "timestamp"))
        writer.writerows(rows)
    events, _ = load_events(training_path)
    train = [e for e in events if e.timestamp_ms < cutoff]
    prior = RecentPopular(365).fit(train, cutoff, 4)
    core = ItemCF(100, 100).fit(train, 4)
    blend = CollaborativeBlend(core, prior, .25)
    features = ContentFeatures(("a", "b", "c", "d", "e", "zero"),
                               np.array([[1, 0], [0, 1], [.6, .8], [-1, 0], [1, 0], [0, 0]], dtype=np.float32))
    catalog = dict.fromkeys(features.items, start)
    catalog_path = tmp_path / "datasets/catalog.json"
    _json(catalog_path, catalog)
    original_path = tmp_path / "artifacts/runs/source/series.json"
    original = {"status": "completed", "protocol_id": "c"*16, "configuration": {"train_end_ms": cutoff}}
    _json(original_path, original)
    archive_path = tmp_path / "docs/experiments/archive/r06-multi-interest-20260917.json"
    archive = {"status": "completed", "selected_method": "A-frozen-s17",
               "model_training_provenance": {"sample_sha256": _sha(training_path)},
               "data_provenance": {"catalog_path": "datasets/catalog.json"},
               "configuration": {"stage": "R06-multi-interest", "source_run": "artifacts/runs/source",
                   "source_series_sha256": _sha(original_path), "retrieval_k": 200, "history_limit": 50,
                   "content": {"history_decay": .8}, "positive_rating_min": 4, "train_end_ms": cutoff}}
    _json(archive_path, archive)
    queries = [LabelTrap(["a"], ["a", "e"], cutoff+2), LabelTrap([], [], cutoff+2)]
    cf = [blend.rank(q.history, q.seen, AvailableAt(catalog, q.timestamp_ms), 200) for q in queries]
    content = ContentPredictor(features, catalog, prior, device="cpu").rank_many(queries, 200)
    pool = build_pool_inputs(features, catalog, {e.item_id for e in train}, blend.priors, queries, cf, content)
    full_samples = []
    for row, q in enumerate(queries):
        valid = pool.items[row] > 0
        full_samples.append({"history": q.history, "seen": sorted(q.seen), "timestamp_ms": q.timestamp_ms,
                             "collaborative": list(cf[row].items), "content": list(content[row].items),
                             "expected_items": [features.items[int(i)-1] for i in pool.items[row, valid]],
                             "expected_context": pool.contexts[row].tolist(), "expected_scalars": pool.scalars[row, valid].tolist()})
    provenance = dict.fromkeys(PROVENANCE_HASHES, "a"*64)
    provenance.update(feature_fingerprint=features.fingerprint, selection_series_sha256=_sha(archive_path),
                      source_series_sha256=_sha(original_path), training_sample_sha256=_sha(training_path),
                      catalog_sha256=_sha(catalog_path), selection_protocol_id="b"*16, training_protocol_id="c"*16)
    metadata = [{"item_id": item, "first_seen_ms": catalog[item], "training_item": item in {e.item_id for e in train},
                 "prior_strength": blend.priors.get(item, 0.)} for item in features.items]
    feature_root = tmp_path / "artifacts/features"
    frozen = write_features(feature_root, features, metadata, provenance, full_samples)
    # Export genuine Torch float32 references, not runtime-derived score goldens.
    model = ResidualListRanker(2, hidden=8, bottleneck=4)
    with torch.no_grad():
        for p in model.parameters():
            p.copy_(torch.linspace(-.15, .15, p.numel()).reshape(p.shape))
    width = pool.items.shape[1]
    vectors = np.vstack((np.zeros((1, 2), dtype=np.float32), features.vectors))
    refs = validation_samples(model, pool.contexts, vectors[pool.items], pool.scalars, pool.items > 0)
    assert width == 400
    ranker_provenance = {key: provenance.get(key, "a"*64) for key in RANKER_HASHES}
    ranker_provenance.update(selected_method="A-frozen-s17", selection_protocol_id="b"*16, training_protocol_id="c"*16)
    ranker_root = tmp_path / "artifacts/ranker"
    ranker = write_component(model, ranker_provenance, refs, ranker_root)
    keys = ("history", "seen", "timestamp_ms", "collaborative", "content")
    samples = [{k: s[k] for k in keys} for s in full_samples]
    fit = {"end_ms": cutoff, "training_rows": len(train), "positive_rating_min": 4}
    return (frozen, prior.counts, core.neighbors, fit, samples, full_samples,
            feature_root, ranker_root, ranker.manifest_sha256, archive_path)


def test_full_export_matches_research_cf_content_and_genuine_torch_ranker(tmp_path):
    f, counts, neighbors, fit, samples, full, feature_root, ranker_root, ranker_hash, _ = _project(tmp_path)
    result = module.export(tmp_path, tmp_path / "artifacts/retrieval", feature_root, f.manifest_sha256, ranker_root, ranker_hash)
    assert result["status"] == "passed" and result["cf_full_order_exact"] and result["content_full_order_exact"]
    assert result["top20_exact"] and result["max_ranker_score_error"] < 1e-5
    assert not result["activated"] and not result["test_queries_evaluated"]
    assert result["cf_statistics_reconstructed"] and result["retrieval_recomputed"]
    assert result["fit"] == fit and fit["training_rows"] == 11
    assert "e" not in counts and "e" not in neighbors
    r = load_r06_retrieval(tmp_path / "artifacts/retrieval", f, expected_manifest_sha256=result["manifest_sha256"])
    for reference in samples:
        output = r.retrieve(reference["history"], reference["seen"], reference["timestamp_ms"])
        assert output.collaborative == tuple(reference["collaborative"])
        assert output.content == tuple(reference["content"])


@pytest.mark.parametrize("field,value", [("selected_method", "B-frozen-s17"), ("status", "running")])
def test_archived_selection_cannot_drift(tmp_path, field, value):
    f, _, _, _, _, _, feature_root, ranker_root, ranker_hash, path = _project(tmp_path)
    archive = json.loads(path.read_text())
    archive[field] = value
    _json(path, archive)
    with pytest.raises(ValueError):
        module.export(tmp_path, tmp_path / "artifacts/retrieval", feature_root, f.manifest_sha256, ranker_root, ranker_hash)


@pytest.mark.parametrize("failure", ["reference-order", "ranker-digest", "interrupted", "report-write", "report-race"])
def test_failures_revoke_only_new_manifest_and_owned_report(tmp_path, monkeypatch, failure):
    f, counts, neighbors, fit, samples, full, _, ranker, digest, _ = _project(tmp_path)
    output, report = tmp_path / "artifacts/retrieval", tmp_path / "report.json"
    if failure == "reference-order":
        samples[0]["content"] = samples[0]["content"][::-1]
    elif failure == "ranker-digest":
        digest = "0"*64
    elif failure == "interrupted":
        monkeypatch.setattr(module, "_verify_ranker", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    elif failure == "report-write":
        original = module._json_bytes
        def broken(value):
            if isinstance(value, dict) and value.get("status") == "passed":
                raise OSError("synthetic disk failure after exclusive report creation")
            return original(value)
        monkeypatch.setattr(module, "_json_bytes", broken)
    else:
        original = Path.open
        def raced(path, mode="r", *args, **kwargs):
            if path == report and mode == "xb":
                with original(path, "wb") as stream:
                    stream.write(b"other writer")
            return original(path, mode, *args, **kwargs)
        monkeypatch.setattr(Path, "open", raced)
    with pytest.raises((ControlledLoadError, ValueError, OSError, KeyboardInterrupt)):
        module._finish_export(output, report, f, counts, neighbors, fit, samples, full, ranker, digest, {})
    assert not (output / "manifest.json").exists()
    if failure == "report-race":
        assert report.read_bytes() == b"other writer"
    else:
        assert not report.exists()
    assert (tmp_path / "artifacts/features/manifest.json").exists()
    assert (ranker / "manifest.json").exists()


def test_existing_component_and_report_are_never_overwritten(tmp_path):
    f, counts, neighbors, fit, samples, full, _, ranker, digest, _ = _project(tmp_path)
    output, report = tmp_path / "artifacts/retrieval", tmp_path / "report.json"
    module.write_retrieval(output, f, counts, neighbors, fit, samples)
    manifest = (output / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        module._finish_export(output, report, f, counts, neighbors, fit, samples, full, ranker, digest, {})
    assert (output / "manifest.json").read_bytes() == manifest
    report.write_bytes(b"old evidence")
    with pytest.raises(FileExistsError):
        module._finish_export(tmp_path / "new", report, f, counts, neighbors, fit, samples, full, ranker, digest, {})
    assert report.read_bytes() == b"old evidence" and not (tmp_path / "new").exists()


def test_export_rejects_training_byte_change_and_outside_destination(tmp_path):
    f, _, _, _, _, _, feature_root, ranker_root, ranker_hash, _ = _project(tmp_path)
    with pytest.raises(ValueError, match="subdirectory"):
        module.export(tmp_path, tmp_path / "outside", feature_root, f.manifest_sha256, ranker_root, ranker_hash)
    path = tmp_path / "datasets/video_games_r03.csv"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        module.export(tmp_path, tmp_path / "artifacts/retrieval", feature_root, f.manifest_sha256, ranker_root, ranker_hash)


@pytest.mark.parametrize("dimension", [2, 128])
def test_randomized_200_item_providers_match_independent_research_oracles(tmp_path, dimension):
    random = np.random.default_rng(1706)
    ids = tuple(f"item{i:04}" for i in range(450))
    vectors = random.normal(size=(450, dimension)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1)[:, None]
    vectors[-1] = 0.
    features = ContentFeatures(ids, vectors)
    events = [Event(f"user{u}", ids[int(i)], 4 if j % 4 else 1, 1+j)
              for u in range(120) for j, i in enumerate(random.choice(430, 12, replace=False))]
    prior = RecentPopular(365).fit(events, 100, 4)
    core = ItemCF(100, 100).fit(events, 4)
    blend = CollaborativeBlend(core, prior, .25)
    catalog = {item: 1 if i < 440 else 200 for i, item in enumerate(ids)}
    queries = [LabelTrap([ids[1], "unknown", ids[2]], [ids[1], "unknown", ids[2], ids[10]], 200),
               LabelTrap([], [], 201), LabelTrap([ids[3]]*4 + [ids[4]], [ids[3], ids[4]], 201),
               LabelTrap([ids[-1]], [ids[-1]], 201)]
    contexts = features.histories([q.history for q in queries])
    cf = [blend.rank(q.history, q.seen, AvailableAt(catalog, q.timestamp_ms), 200) for q in queries]
    full, samples = [], []
    for row, query in enumerate(queries):
        # NumPy float32 cumulative sum independently expresses the declared
        # sequential arithmetic. It is not a call to the runtime score helper.
        scores = np.cumsum(vectors * contexts[row], axis=1, dtype=np.float32)[:, -1]
        legal = [i for i, item in enumerate(ids) if catalog[item] < query.timestamp_ms
                 and item not in query.seen and features.present[i]]
        right = [ids[i] for i in sorted(legal, key=lambda i: (-float(scores[i]), ids[i]))[:200]] if np.linalg.norm(contexts[row]) > 1e-8 else []
        fallback = prior.rank((), query.seen, AvailableAt(catalog, query.timestamp_ms), 200).items
        right = (right + [i for i in fallback if i not in right])[:200]
        full.append(Ranking(tuple(right)))
        samples.append({"history": query.history, "seen": sorted(query.seen), "timestamp_ms": query.timestamp_ms,
                        "collaborative": list(cf[row].items), "content": right})
    pools = build_pool_inputs(features, catalog, {e.item_id for e in events}, blend.priors, queries, cf, full)
    references = []
    for row, sample in enumerate(samples):
        valid = pools.items[row] > 0
        references.append({**sample, "expected_items": [ids[int(i)-1] for i in pools.items[row, valid]],
                           "expected_context": pools.contexts[row].tolist(), "expected_scalars": pools.scalars[row, valid].tolist()})
    provenance = dict.fromkeys(PROVENANCE_HASHES, "a"*64)
    provenance.update(feature_fingerprint=features.fingerprint, selection_protocol_id="b"*16, training_protocol_id="c"*16)
    training_items = {e.item_id for e in events}
    metadata = [{"item_id": item, "first_seen_ms": catalog[item], "training_item": item in training_items,
                 "prior_strength": blend.priors.get(item, 0.)} for item in ids]
    frozen = write_features(tmp_path / "features", features, metadata, provenance, references)
    runtime = module.write_retrieval(tmp_path / "retrieval", frozen, prior.counts, core.neighbors,
                                    {"end_ms": 100, "training_rows": len(events), "positive_rating_min": 4}, samples)
    for sample in samples:
        result = runtime.retrieve(sample["history"], sample["seen"], sample["timestamp_ms"])
        assert len(result.collaborative) == len(result.content) == 200
        assert result.collaborative == tuple(sample["collaborative"])
        assert result.content == tuple(sample["content"])
