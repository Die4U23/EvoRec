"""Synthetic fitted sklearn references; no private weights required by tests."""

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("sklearn")
pytest.importorskip("torch")
import joblib
import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize

from evorec.infrastructure.content_encoder import PROVENANCE_HASHES, load_content_encoder
from evorec.infrastructure.residual_ranker import ControlledLoadError
from evorec.research.content import ContentFeatures
import evorec.research.export_encoder as module


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _model():
    texts = ["alpha beta café 你好", "alpha alpha beta", "beta gamma 你好", "gamma café café",
             "alpha gamma café", "beta gamma café 你好"]
    vectorizer = TfidfVectorizer(max_features=20000, min_df=2, ngram_range=(1, 2),
                                sublinear_tf=True, dtype=np.float32)
    matrix = vectorizer.fit_transform(texts)
    svd = TruncatedSVD(n_components=2, n_iter=7, random_state=17).fit(matrix)
    return {"vectorizer": vectorizer, "svd": svd}


def _samples(model):
    texts = ["", "ALPHA alpha beta", "alpha OOV beta", "café cafe\u0301 你好", "xxzzzzzz"]
    expected = normalize(model["svd"].transform(model["vectorizer"].transform(texts))).astype(np.float32)
    return [{"text": text, "expected_vector": vector.tolist()} for text, vector in zip(texts, expected, strict=True)]


def _provenance():
    value = dict.fromkeys(PROVENANCE_HASHES, "a"*64)
    value.update(selected_method="A-frozen-s17", selection_protocol_id="b"*16,
                 training_protocol_id="c"*16, encoder_protocol_id="d"*16)
    return value


FIT = {"end_ms": 1000, "training_rows": 6, "document_count": 6}


def _project(tmp_path):
    source = tmp_path / "artifacts/source"
    encoder = source / "content-encoder"
    encoder.mkdir(parents=True)
    model = _model()
    metadata = {"synthetic-a": "alpha beta", "synthetic-b": "café 你好",
                "synthetic-c": "gamma beta", "synthetic-d": "alpha gamma"}
    metadata_path = tmp_path / "datasets/metadata.json"
    _json(metadata_path, metadata)
    items = sorted(metadata)
    vectors = normalize(model["svd"].transform(model["vectorizer"].transform(list(metadata.values())))).astype(np.float32)
    features = ContentFeatures(items, vectors)
    _json(encoder / "items.json", items)
    np.save(encoder / "vectors.npy", vectors)
    joblib.dump(model, encoder / "encoder.joblib")
    content = {"fields": ["title", "categories"], "access_assumption": "synthetic-static-metadata",
               "dimensions": 2, "max_features": 20000, "min_df": 2, "svd_seed": 17, "history_decay": .8}
    meta = {"metadata_sha256": _sha(metadata_path), "fields": ["title", "categories"],
            "access_assumption": content["access_assumption"]}
    original = {"status": "completed", "protocol_id": "c"*16, "feature_fingerprint": features.fingerprint,
                "configuration": {"content": content, "metadata_path": "datasets/metadata.json"},
                "metadata_provenance": meta, "model_training_provenance": {"sample_sha256": "a"*64},
                "content_encoder": {"files": {name: _sha(encoder / name) for name in ("encoder.joblib", "items.json", "vectors.npy")},
                    "encoder_reload_verified": True, "fit_end_ms": 1000, "fit_training_rows": 6,
                    "fit_document_count": 6, "fit_item_set_sha256": "e"*64,
                    "fit_training_signature": "f"*64, "protocol_id": "d"*16,
                    "vocabulary_terms": len(model["vectorizer"].vocabulary_), "dimensions": 2}}
    _json(source / "series.json", original)
    archive = {"status": "completed", "selected_method": "A-frozen-s17", "protocol_id": "b"*16,
               "configuration": {"source_run": "artifacts/source", "source_series_sha256": _sha(source / "series.json"),
                                 "stage": "R06-multi-interest", "content": content, "train_end_ms": 2000},
               "feature_fingerprint": features.fingerprint, "metadata_provenance": meta,
               "test_results": "not evaluated", "test_candidates": "not opened"}
    path = tmp_path / "docs/experiments/archive/r06-multi-interest-20260917.json"
    _json(path, archive)
    return path, archive, source


def test_sklearn_roundtrip_and_no_overwrite(tmp_path):
    model = _model()
    samples = _samples(model)
    root = tmp_path / "component"
    runtime = module.write_encoder(root, **model, provenance=_provenance(), fit=FIT, samples=samples)
    for sample in samples:
        assert runtime.encode(sample["text"]) == pytest.approx(sample["expected_vector"], abs=1e-6)
    assert runtime.vocabulary_terms == len(model["vectorizer"].vocabulary_)
    before = _sha(root / "manifest.json")
    with pytest.raises(FileExistsError):
        module.write_encoder(root, **model, provenance=_provenance(), fit=FIT, samples=samples)
    assert _sha(root / "manifest.json") == before


def test_complete_synthetic_export_does_not_fit_or_read_labels(tmp_path, monkeypatch):
    _, _, source = _project(tmp_path)
    before = _sha(source / "content-encoder/encoder.joblib")
    def forbidden(*args, **kwargs):
        raise AssertionError("must not refit")
    monkeypatch.setattr(TfidfVectorizer, "fit", forbidden)
    monkeypatch.setattr(TfidfVectorizer, "fit_transform", forbidden)
    monkeypatch.setattr(TruncatedSVD, "fit", forbidden)
    output = tmp_path / "artifacts/component"
    result = module.export(tmp_path, output, allow_trusted_joblib=True)
    assert result["component_only"] and not result["activated"] and not result["retrained"]
    assert not result["test_queries_evaluated"] and not result["retrieval_recomputed"]
    assert result["max_absolute_vector_error"] <= 1e-6
    assert result["max_stored_catalog_vector_error"] <= 1e-6
    assert _sha(source / "content-encoder/encoder.joblib") == before
    report = output.with_name("component-verification.json")
    assert json.loads(report.read_text()) == result
    for relative, digest in result["export_code"]["source_sha256"].items():
        assert _sha(output.with_name("component-source") / relative) == digest
    with pytest.raises(FileExistsError):
        module.export(tmp_path, output, allow_trusted_joblib=True)


def test_legacy_load_requires_acknowledgement_and_checked_bytes(tmp_path, monkeypatch):
    _, _, source = _project(tmp_path)
    output = tmp_path / "artifacts/component"
    with pytest.raises(ValueError, match="acknowledgement"):
        module.export(tmp_path, output)
    path = source / "content-encoder/encoder.joblib"
    digest = _sha(path)
    path.write_bytes(b"changed after initial path check")
    monkeypatch.setattr(joblib, "load", lambda *a, **kw: pytest.fail("must not deserialize changed bytes"))
    with pytest.raises(ValueError, match="frozen hash"):
        module._trusted_model(path, digest)
    assert not output.exists()


@pytest.mark.parametrize("artifact", ["series.json", "content-encoder/encoder.joblib", "content-encoder/items.json", "content-encoder/vectors.npy", "metadata"])
def test_all_source_hashes_are_checked_before_deserialization(tmp_path, monkeypatch, artifact):
    _, _, source = _project(tmp_path)
    path = tmp_path / "datasets/metadata.json" if artifact == "metadata" else source / artifact
    path.write_bytes(b"changed")
    monkeypatch.setattr(joblib, "load", lambda *a, **kw: pytest.fail("must not deserialize before hash checks"))
    with pytest.raises(ValueError, match="hash mismatch"):
        module.export(tmp_path, tmp_path / "artifacts/component", allow_trusted_joblib=True)


@pytest.mark.parametrize("field,value", [("status", "running"), ("selected_method", "B-frozen-s17"), ("feature_fingerprint", "0"*64)])
def test_source_selection_cannot_drift(tmp_path, field, value):
    path, archive, _ = _project(tmp_path)
    archive[field] = value
    _json(path, archive)
    with pytest.raises(ValueError):
        module.export(tmp_path, tmp_path / "artifacts/component", allow_trusted_joblib=True)


@pytest.mark.parametrize("key,value", [("fields", ["reviews"]), ("dimensions", 3), ("max_features", 10000)])
def test_source_content_configuration_is_bound_to_transform(tmp_path, key, value):
    path, archive, source = _project(tmp_path)
    original_path = source / "series.json"
    original = json.loads(original_path.read_text())
    original["configuration"]["content"][key] = value
    archive["configuration"]["content"][key] = value
    _json(original_path, original)
    archive["configuration"]["source_series_sha256"] = _sha(original_path)
    _json(path, archive)
    with pytest.raises(ValueError):
        module.export(tmp_path, tmp_path / "artifacts/component", allow_trusted_joblib=True)


@pytest.mark.parametrize("key,value", [("lowercase", False), ("binary", True), ("norm", None),
    ("token_pattern", r"\w+"), ("strip_accents", "unicode"), ("sublinear_tf", 1)])
def test_export_rejects_analyzer_drift(tmp_path, key, value):
    model = _model()
    samples = _samples(model)
    model["vectorizer"].set_params(**{key: value})
    with pytest.raises(ValueError, match="frozen protocol"):
        module.write_encoder(tmp_path / "component", **model, provenance=_provenance(), fit=FIT, samples=samples)


def test_failed_reference_and_interrupt_revoke_new_manifest(tmp_path, monkeypatch):
    model = _model()
    samples = _samples(model)
    samples[1]["expected_vector"] = [1., 0.]
    root = tmp_path / "failed"
    with pytest.raises(ControlledLoadError):
        module.write_encoder(root, **model, provenance=_provenance(), fit=FIT, samples=samples)
    assert not (root / "manifest.json").exists()
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(module, "load_content_encoder", interrupted)
    root = tmp_path / "interrupted"
    with pytest.raises(KeyboardInterrupt):
        module.write_encoder(root, **model, provenance=_provenance(), fit=FIT, samples=_samples(model))
    assert not (root / "manifest.json").exists()


def test_report_failure_and_binding_failure_revoke_only_new_output(tmp_path, monkeypatch):
    model = _model()
    samples = _samples(model)
    root, report = tmp_path / "component", tmp_path / "report.json"
    original_bytes = module._json_bytes
    def broken_report(value):
        if isinstance(value, dict) and value.get("status") == "passed":
            raise OSError("synthetic report write failure")
        return original_bytes(value)
    monkeypatch.setattr(module, "_json_bytes", broken_report)
    with pytest.raises(OSError):
        module._finish_export(root, report, model, _provenance(), FIT, samples, {})
    assert not report.exists() and not (root / "manifest.json").exists()
    monkeypatch.setattr(module, "_json_bytes", original_bytes)
    def mismatch(*args, **kwargs):
        raise ControlledLoadError("component_changed", "synthetic binding failure")
    monkeypatch.setattr(module, "load_r06_features", mismatch)
    root = tmp_path / "paired"
    with pytest.raises(ControlledLoadError):
        module._finish_export(root, report, model, _provenance(), FIT, samples, {}, tmp_path / "features", "a"*64)
    assert not report.exists() and not (root / "manifest.json").exists()
    report.write_bytes(b"existing evidence")
    with pytest.raises(FileExistsError):
        module._finish_export(tmp_path / "new", report, model, _provenance(), FIT, samples, {})
    assert report.read_bytes() == b"existing evidence" and not (tmp_path / "new").exists()


def test_report_creation_race_preserves_other_writer(tmp_path, monkeypatch):
    model = _model()
    root, report = tmp_path / "component", tmp_path / "report.json"
    original_open = Path.open
    def race(path, mode="r", *args, **kwargs):
        if path == report and mode == "xb":
            with original_open(report, "wb") as stream:
                stream.write(b"other writer")
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", race)
    with pytest.raises(FileExistsError):
        module._finish_export(root, report, model, _provenance(), FIT, _samples(model), {})
    assert report.read_bytes() == b"other writer"
    assert not (root / "manifest.json").exists()


def test_destination_escape_and_partial_pair_are_rejected(tmp_path):
    _project(tmp_path)
    with pytest.raises(ValueError, match="artifacts subdirectory"):
        module.export(tmp_path, tmp_path / "outside", allow_trusted_joblib=True)
    with pytest.raises(ValueError, match="supplied together"):
        module.export(tmp_path, tmp_path / "artifacts/component", allow_trusted_joblib=True, features_component=tmp_path)
