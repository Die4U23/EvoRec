"""Synthetic research references, label traps and fail-closed joint exports."""

import hashlib
import json
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("sklearn")
import numpy as np

from evorec.infrastructure.r06_features import PROVENANCE_HASHES, load_r06_features
from evorec.infrastructure.residual_ranker import ControlledLoadError, PROVENANCE_HASHES as RANKER_HASHES
from evorec.research.baselines import Ranking
from evorec.research.content import ContentFeatures
from evorec.research.export_features import _finish_export, write_features
from evorec.research.export_ranker import validation_samples, write_component
from evorec.research.ranker import ResidualListRanker, build_pool_inputs


class LabelTrap:
    def __init__(self, history, seen):
        self.history, self.seen, self.timestamp_ms = history, frozenset(seen), 86400001

    @property
    def target(self):
        pytest.fail("feature construction must not read the target")

    target_model_cold = target
    target_available = target


def _inputs(history=(), extra_seen=()):
    features = ContentFeatures(("a", "b", "c", "opposite", "tiny", "zero"),
                               np.array([[1, 0], [0, 1], [.6, .8], [-1, 0], [1e-7, 0], [0, 0]], dtype=np.float32))
    catalog = dict.fromkeys(features.items, 1)
    training = {"a", "b"}  # b has only low-rating training interactions, hence no positive prior.
    priors = {"a": 1.}
    query = LabelTrap(list(history), (*history, *extra_seen))
    cf = tuple(item for item in ("zero", "b", "c", "tiny") if item not in query.seen)
    content = tuple(item for item in ("c", "b", "zero") if item not in query.seen)
    original = build_pool_inputs(features, catalog, training, priors, [query], [Ranking(cf)], [Ranking(content)])
    valid = original.items[0] > 0
    sample = {"history": query.history, "seen": sorted(query.seen), "timestamp_ms": query.timestamp_ms,
              "collaborative": list(cf), "content": list(content),
              "expected_items": [features.items[int(index)-1] for index in original.items[0, valid]],
              "expected_context": original.contexts[0].tolist(),
              "expected_scalars": original.scalars[0, valid].tolist()}
    metadata = [{"item_id": item, "first_seen_ms": catalog[item], "training_item": item in training,
                 "prior_strength": priors.get(item, 0.)} for item in features.items]
    provenance = dict.fromkeys(PROVENANCE_HASHES, "a" * 64)
    provenance.update(feature_fingerprint=features.fingerprint, selection_protocol_id="b" * 16,
                      training_protocol_id="c" * 16)
    return features, metadata, provenance, [sample]


@pytest.mark.parametrize("history", [(), ("a",), ("unknown",), ("zero",), ("tiny",),
                                     ("a", "unknown", "b"), ("a", "zero", "b"), ("a", "a"),
                                     ("a", "opposite"), tuple(["a", "unknown", "b", "zero", "tiny"]*10)])
def test_roundtrip_matches_original_float32_builder_and_never_reads_labels(tmp_path, history):
    features, metadata, provenance, samples = _inputs(history, extra_seen=("b",))
    runtime = write_features(tmp_path / "features", features, metadata, provenance, samples)
    sample = samples[0]
    pool = runtime.build_pool(*(sample[key] for key in ("history", "seen", "timestamp_ms", "collaborative", "content")))
    assert pool.item_ids == tuple(sample["expected_items"])
    np.testing.assert_allclose(pool.context, sample["expected_context"], atol=1e-6, rtol=0)
    np.testing.assert_allclose(pool.scalars, sample["expected_scalars"], atol=1e-6, rtol=0)
    for item, vector in zip(pool.item_ids, pool.candidates, strict=True):
        assert vector == tuple(features.vectors[features.mapping[item]])
    if history == ("tiny",):
        assert 0 < pool.context[0] < 1e-6  # sklearn's tiny norm is not scaled up to a unit vector.
    assert not set(sample) & {"target", "target_available", "target_model_cold"}


def _ranker(tmp_path, parts, *, wrong_source=False, different_context=False):
    features, _, provenance, samples = parts
    sample = samples[0]
    context = np.array([sample["expected_context"]], dtype=np.float32)
    if different_context:
        context[:] = [0, 1]
    candidates = np.array([[features.vectors[features.mapping[item]] for item in sample["expected_items"]]], dtype=np.float32)
    scalars = np.array([sample["expected_scalars"]], dtype=np.float32)
    model = ResidualListRanker(2, hidden=8, bottleneck=4)
    with torch.no_grad():
        for parameter in model.parameters():
            parameter.copy_(torch.linspace(-.15, .15, parameter.numel()).reshape(parameter.shape))
    references = validation_samples(model, context, candidates, scalars, np.ones(scalars.shape[:2], dtype=bool))
    ranker_provenance = {key: provenance.get(key, "a"*64) for key in RANKER_HASHES}
    ranker_provenance.update(selected_method="A-frozen-s17", selection_protocol_id=provenance["selection_protocol_id"],
                             training_protocol_id=provenance["training_protocol_id"])
    if wrong_source:
        ranker_provenance["source_series_sha256"] = "0"*64
    root = tmp_path / "ranker"
    runtime = write_component(model, ranker_provenance, references, root)
    return root, runtime.manifest_sha256


def test_joint_export_replays_genuine_torch_reference_and_records_no_activation(tmp_path):
    parts = _inputs(("a",))
    ranker, digest = _ranker(tmp_path, parts)
    report = tmp_path / "verification.json"
    result = _finish_export(tmp_path / "features", report, *parts, ranker, digest, {"synthetic": True})
    assert json.loads(report.read_text()) == result
    assert result["status"] == "passed" and result["top20_exact"] and result["synthetic"]
    assert result["max_ranker_score_error"] < 1e-5
    assert not result["activated"] and not result["test_queries_evaluated"]
    assert not result["retrieval_recomputed"] and not result["new_text_encoded"]
    assert load_r06_features(tmp_path / "features", expected_manifest_sha256=result["manifest_sha256"])


@pytest.mark.parametrize("failure", ["wrong-digest", "wrong-source", "different-context", "report-write", "interrupted"])
def test_joint_failure_revokes_manifest_and_no_passed_report_remains(tmp_path, monkeypatch, failure):
    parts = _inputs(("a",))
    ranker, digest = _ranker(tmp_path, parts, wrong_source=failure == "wrong-source",
                           different_context=failure == "different-context")
    report, output = tmp_path / "verification.json", tmp_path / "features"
    if failure == "wrong-digest":
        digest = "0"*64
    if failure in ("report-write", "interrupted"):
        original = Path.write_bytes
        def broken_write(path, raw):
            if path == report:
                original(path, b'{"status":"passed"')  # Interrupted write must not survive as evidence.
                if failure == "interrupted":
                    raise KeyboardInterrupt("synthetic user interruption")
                raise OSError("synthetic disk failure")
            return original(path, raw)
        monkeypatch.setattr(Path, "write_bytes", broken_write)
    with pytest.raises((ControlledLoadError, ValueError, OSError, KeyboardInterrupt)):
        _finish_export(output, report, *parts, ranker, digest, {})
    assert not (output / "manifest.json").exists()
    assert not report.exists()
    with pytest.raises(ControlledLoadError):
        load_r06_features(output)


def test_failed_reference_and_existing_component_are_not_overwritten(tmp_path):
    parts = _inputs(("a",))
    output = tmp_path / "features"
    runtime = write_features(output, *parts)
    manifest = (output / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        _finish_export(output, tmp_path / "report.json", *parts, tmp_path / "missing", "0"*64, {})
    assert (output / "manifest.json").read_bytes() == manifest
    assert hashlib.sha256(manifest).hexdigest() == runtime.manifest_sha256
    parts[-1][0]["expected_scalars"][0][4] = 1.
    failed = tmp_path / "failed"
    with pytest.raises(ControlledLoadError, match="scalar features differ"):
        write_features(failed, *parts)
    assert not (failed / "manifest.json").exists()


def test_existing_report_is_preserved_before_component_creation(tmp_path):
    report = tmp_path / "report.json"
    report.write_bytes(b"previous evidence")
    with pytest.raises(FileExistsError):
        _finish_export(tmp_path / "features", report, *_inputs(), tmp_path / "missing", "0"*64, {})
    assert report.read_bytes() == b"previous evidence"
    assert not (tmp_path / "features").exists()


@pytest.mark.parametrize("dimension,count,provider_k", [(128, 64, 20), (2, 450, 200)])
def test_randomized_full_dimension_and_maximum_union_parity(tmp_path, dimension, count, provider_k):
    random = np.random.default_rng(1706)
    vectors = random.normal(size=(count, dimension)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1)[:, None]
    vectors[-1] = 0
    vectors[-2] = 0
    vectors[-2, 0] = 1e-7
    features = ContentFeatures(tuple(f"item-{i:03}" for i in range(count)), vectors)
    catalog = {item: i+1 for i, item in enumerate(features.items)}
    training = set(features.items[:count//2])
    priors = {item: float(random.random()) for item in training}
    histories = [[], [features.items[-2]], [features.items[-1]], ["unknown"],
                 [features.items[0], "unknown", features.items[1]]]
    histories.extend(random.choice(features.items[:8], size=length).tolist() for length in (2, 3, 5, 10, 25, 50))
    queries = [LabelTrap(history, (*history, features.items[10])) for history in histories]
    cf, content = [], []
    for query in queries:
        available = [item for item in features.items if item not in query.seen]
        ordered = random.permutation(available).tolist()
        cf.append(Ranking(tuple(ordered[:provider_k])))
        content.append(Ranking(tuple(ordered[-provider_k:])))
    original = build_pool_inputs(features, catalog, training, priors, queries, cf, content)
    samples = []
    for row, query in enumerate(queries[:4]):
        valid = original.items[row] > 0
        samples.append({"history": query.history, "seen": sorted(query.seen), "timestamp_ms": query.timestamp_ms,
                        "collaborative": list(cf[row].items), "content": list(content[row].items),
                        "expected_items": [features.items[int(i)-1] for i in original.items[row, valid]],
                        "expected_context": original.contexts[row].tolist(),
                        "expected_scalars": original.scalars[row, valid].tolist()})
    provenance = dict.fromkeys(PROVENANCE_HASHES, "a"*64)
    provenance.update(feature_fingerprint=features.fingerprint, selection_protocol_id="b"*16,
                      training_protocol_id="c"*16)
    metadata = [{"item_id": item, "first_seen_ms": catalog[item], "training_item": item in training,
                 "prior_strength": priors.get(item, 0.)} for item in features.items]
    runtime = write_features(tmp_path / "features", features, metadata, provenance, samples)
    for row, query in enumerate(queries):
        pool = runtime.build_pool(query.history, query.seen, query.timestamp_ms, cf[row].items, content[row].items)
        valid = original.items[row] > 0
        assert pool.item_ids == tuple(features.items[int(i)-1] for i in original.items[row, valid])
        np.testing.assert_allclose(pool.context, original.contexts[row], atol=1e-6, rtol=0)
        np.testing.assert_allclose(pool.scalars, original.scalars[row, valid], atol=1e-6, rtol=0)
        if provider_k == 200:
            assert len(pool.item_ids) == 400
