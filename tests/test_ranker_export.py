"""Research-environment export tests with explicitly synthetic checkpoints."""

import hashlib
import json

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("sklearn")
import numpy as np

from evorec.infrastructure.model_runtime import ControlledLoadError
from evorec.research.content import ContentFeatures
from evorec.research.export_ranker import export, validation_samples, write_component
from evorec.research.ranker import ResidualListRanker, SCALAR_NAMES


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _record(project, path):
    return {"path_from_project_root": path.relative_to(project).as_posix(), "sha256": _hash(path)}


def _project(tmp_path):
    source = tmp_path / "artifacts/source"
    replica = tmp_path / "artifacts/replica"
    encoder = source / "content-encoder"
    encoder.mkdir(parents=True)
    replica.mkdir()
    features = ContentFeatures(("synthetic-a", "synthetic-b", "synthetic-c"),
                               np.array([[1, 0], [0, 1], [.6, .8]], dtype=np.float32))
    _json(encoder / "items.json", list(features.items))
    np.save(encoder / "vectors.npy", features.vectors)
    config = {"hidden": 8, "bottleneck": 4, "base_scale": 8, "residual_scale": 4}
    model = ResidualListRanker(2, **config)
    with torch.no_grad():
        for parameter in model.parameters():
            parameter.copy_(torch.linspace(-.15, .15, parameter.numel()).reshape(parameter.shape))
    checkpoint = replica / "best.pt"
    torch.save({"protocol_id": "a" * 16, "feature_fingerprint": features.fingerprint,
                "scalar_names": list(SCALAR_NAMES), "model_config": config,
                "state_dict": model.state_dict()}, checkpoint)
    original = {"status": "completed", "protocol_id": "a" * 16, "configuration": {"model": config},
                "feature_fingerprint": features.fingerprint,
                "content_encoder": {"files": {name: _hash(encoder / name)
                                               for name in ("items.json", "vectors.npy")}}}
    _json(source / "series.json", original)
    checkpoint_record = _record(tmp_path, checkpoint)
    _json(replica / "series.json", {"status": "completed", "trials": [{"seed": 17,
                                                                         "checkpoint": checkpoint_record}]})
    ids = np.zeros((4, 400), dtype=np.int32)
    ids[:, :3] = [1, 2, 3]
    contexts = np.array([[1, 0], [0, 1], [.6, .8], [0, 0]], dtype=np.float32)
    scalars = np.zeros((4, 400, 8), dtype=np.float32)
    scalars[:, :3, 1] = [1, .5, .25]
    scalars[:, :3, 2] = [.25, .5, 1]
    pool = tmp_path / "artifacts/validation-A.npz"
    # Poison fields prove export does not read labels or query identities (allow_pickle=False).
    np.savez(pool, items=ids, contexts=contexts, scalars=scalars,
             targets=np.array([object()], dtype=object), query_ids=np.array([object()], dtype=object))
    archive = {
        "status": "completed", "protocol_id": "b" * 16, "selected_method": "A-frozen-s17",
        "configuration": {"stage": "R06-multi-interest", "model": config,
                          "source_run": "artifacts/source", "source_replication": "artifacts/replica",
                          "source_series_sha256": _hash(source / "series.json"),
                          "source_replication_sha256": _hash(replica / "series.json")},
        "feature_fingerprint": features.fingerprint, "frozen_checkpoints": {"17": checkpoint_record},
        "validation_candidates": {"caches": {"A": _record(tmp_path, pool)}},
        "test_results": "DO NOT USE",
    }
    archive_path = tmp_path / "docs/experiments/archive/r06-multi-interest-20260917.json"
    _json(archive_path, archive)
    return archive_path, archive, source, replica, model


def test_synthetic_export_replays_torch_padding_and_empty_context(tmp_path):
    archive_path, archive, _, _, _ = _project(tmp_path)
    output = tmp_path / "artifacts/component"
    result = export(tmp_path, output)
    assert result["component_only"] and not result["activated"] and not result["retrained"]
    assert result["top20_exact"] and result["candidate_counts"] == [3, 3, 3, 3]
    assert not result["test_split_read"]
    assert result["max_absolute_score_error"] <= 1e-5
    assert result["provenance"]["selection_series_sha256"] == _hash(archive_path)
    report = output.with_name(output.name + "-verification.json")
    assert json.loads(report.read_text()) == result
    snapshot = output.with_name(output.name + "-source")
    for relative, digest in result["export_code"]["source_sha256"].items():
        assert _hash(snapshot / relative) == digest
    # Output neither contains raw identifiers nor is allowed to overwrite previous evidence.
    assert b"synthetic-" not in (output / "validation.json").read_bytes()
    with pytest.raises(FileExistsError):
        export(tmp_path, output)


@pytest.mark.parametrize("field,value", [("status", "running"), ("selected_method", "B-frozen-s17"),
                                        ("feature_fingerprint", "0" * 64)])
def test_source_stage_selection_and_features_cannot_drift(tmp_path, field, value):
    path, archive, _, _, _ = _project(tmp_path)
    archive[field] = value
    _json(path, archive)
    with pytest.raises(ValueError):
        export(tmp_path, tmp_path / "artifacts/component")
    assert not (tmp_path / "artifacts/component").exists()


@pytest.mark.parametrize("artifact", ["checkpoint", "source", "pool", "vectors"])
def test_source_hash_drift_is_rejected(tmp_path, artifact):
    _, archive, source, replica, _ = _project(tmp_path)
    path = {"checkpoint": replica / "best.pt", "source": source / "series.json",
            "pool": tmp_path / "artifacts/validation-A.npz", "vectors": source / "content-encoder/vectors.npy"}[artifact]
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        export(tmp_path, tmp_path / "artifacts/component")


def test_export_destination_and_source_path_cannot_escape(tmp_path):
    path, archive, _, _, _ = _project(tmp_path)
    with pytest.raises(ValueError, match="beneath project artifacts"):
        export(tmp_path, tmp_path / "outside")
    archive["frozen_checkpoints"]["17"]["path_from_project_root"] = "../escape.pt"
    _json(path, archive)
    with pytest.raises(ValueError):
        export(tmp_path, tmp_path / "artifacts/component")


def test_failed_golden_replay_leaves_no_loadable_manifest(tmp_path):
    _, _, _, _, model = _project(tmp_path)
    contexts = np.array([[1, 0]], dtype=np.float32)
    candidates = np.array([[[0, 1]]], dtype=np.float32)
    scalars = np.zeros((1, 1, 8), dtype=np.float32)
    samples = validation_samples(model, contexts, candidates, scalars, np.ones((1, 1), dtype=bool))
    samples[0]["expected_scores"][0] += 1
    from evorec.infrastructure.residual_ranker import PROVENANCE_HASHES
    provenance = dict.fromkeys(PROVENANCE_HASHES, "a" * 64)
    provenance.update(selected_method="A-frozen-s17", selection_protocol_id="a" * 16,
                      training_protocol_id="b" * 16)
    output = tmp_path / "artifacts/failed"
    with pytest.raises(ControlledLoadError, match="reference scores differ"):
        write_component(model, provenance, samples, output)
    assert not (output / "manifest.json").exists()


@pytest.mark.parametrize("change", ["activation", "approximation", "dtype"])
def test_export_rejects_architecture_drift_even_with_zero_residual(tmp_path, change):
    _, _, _, _, model = _project(tmp_path)
    if change == "activation":
        model.network[1] = torch.nn.ReLU()
    elif change == "approximation":
        model.network[1] = torch.nn.GELU(approximate="tanh")
    else:
        model.double()
    with pytest.raises(ValueError, match="exact-GELU residual architecture"):
        write_component(model, {}, [], tmp_path / "artifacts/invalid")
