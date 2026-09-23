"""Controlled loading rejects executable formats, mismatches, and drift."""

import hashlib
import json
import struct
from uuid import uuid4

import pytest

from evorec.infrastructure.bundle import validate_bundle
from evorec.infrastructure.model_runtime import (
    ControlledLoadError,
    LoadLimits,
    load_runtime_bundle,
)
from scripts.load_bundle import main


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _bundle(tmp_path):
    managed = tmp_path / "managed"
    bundle_id = str(uuid4())
    root = managed / bundle_id
    root.mkdir(parents=True)
    item_ids = ["item-a", "item-b"]
    mapping = _json([
        {"internal_id": 0, "item_id": "item-a"},
        {"internal_id": 1, "item_id": "item-b"},
    ])
    embeddings = struct.pack("<4f", 1.0, 0.0, 0.0, 1.0)
    ranker = _json({
        "schema_version": 1,
        "kind": "dot-product-v1",
        "model_id": "ranker-v1",
        "dimension": 2,
        "scale": 1.0,
        "biases": [0.0, 0.1],
        "validation_samples": [
            {
                "history_item_ids": ["item-a"],
                "candidate_item_ids": ["item-a", "item-b"],
                "expected_scores": [1.0, 0.1],
                "expected_order": ["item-a", "item-b"],
            },
            {
                "history_item_ids": ["item-b"],
                "candidate_item_ids": ["item-a", "item-b"],
                "expected_scores": [0.0, 1.1],
                "expected_order": ["item-b", "item-a"],
            },
        ],
    })
    encoder = _json({
        "schema_version": 1,
        "kind": "mean-history-v1",
        "model_id": "encoder-v1",
        "dimension": 2,
        "dtype": "float32",
        "output_normalized": True,
    })
    codebook = _json({
        "schema_version": 1,
        "kind": "item-codebook-v1",
        "codebook_id": "codebook-v1",
        "item_count": 2,
        "codes": ["0", "1"],
    })
    index = _json({
        "schema_version": 1,
        "kind": "flat-v1",
        "item_count": 2,
        "dimension": 2,
        "dtype": "float32",
        "normalized": True,
        "distance": "cosine",
        "internal_ids": [0, 1],
    })
    contents = {
        "item_mapping": ("items.json", mapping),
        "item_embeddings": ("embeddings.f32", embeddings),
        "ranker": ("ranker.json", ranker),
        "content_encoder": ("encoder.json", encoder),
        "semantic_codebook": ("codebook.json", codebook),
        "vector_index": ("index.json", index),
    }
    for path, content in contents.values():
        (root / path).write_bytes(content)
    manifest = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "created_at": "2026-09-23T17:00:00+08:00",
        "source": {
            "build_task_id": "runtime-fixture",
            "code_revision": "a" * 40,
            "code_dirty": False,
            "dataset_sha256": "b" * 64,
        },
        "data_protocol": {
            "training_cutoff": "2026-09-22T00:00:00Z",
            "availability_rule": "published_at < request_time",
            "static_metadata_rule": "title and category only",
        },
        "artifacts": {
            "item_mapping_role": "item_mapping",
            "item_embeddings_role": "item_embeddings",
            "ranker_role": "ranker",
            "ranker_id": "ranker-v1",
            "content_encoder_role": "content_encoder",
            "content_encoder_id": "encoder-v1",
            "semantic_codebook_role": "semantic_codebook",
            "semantic_codebook_id": "codebook-v1",
            "vector_index_role": "vector_index",
        },
        "catalog": {
            "item_count": 2,
            "item_set_sha256": _digest(_json(item_ids)),
        },
        "index": {
            "kind": "flat", "dimension": 2, "dtype": "float32",
            "normalized": True, "distance": "cosine", "parameters": {},
        },
        "strategy": {
            "supported_paths": ["content"],
            "retrieval_budget": 2,
            "ranking_budget": 2,
        },
        "files": [
            {"role": role, "path": path, "size_bytes": len(content),
             "sha256": _digest(content)}
            for role, (path, content) in contents.items()
        ],
    }
    _write_manifest(root, manifest)
    return managed, root, manifest


def _write_manifest(root, manifest):
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _replace(root, manifest, role, content):
    entry = next(value for value in manifest["files"] if value["role"] == role)
    (root / entry["path"]).write_bytes(content)
    entry["size_bytes"] = len(content)
    entry["sha256"] = _digest(content)
    _write_manifest(root, manifest)


def _document(root, manifest, role):
    entry = next(value for value in manifest["files"] if value["role"] == role)
    return json.loads((root / entry["path"]).read_bytes())


def test_controlled_load_replays_samples_and_scores_known_items(tmp_path):
    managed, root, _ = _bundle(tmp_path)
    runtime = load_runtime_bundle(validate_bundle(managed, root))
    assert runtime.item_ids == ("item-a", "item-b")
    assert runtime.validation_samples_checked == 2
    assert runtime.score(["item-a"], ["item-a", "item-b"]) == pytest.approx((1.0, 0.1))
    with pytest.raises(ValueError, match="unknown item ID"):
        runtime.score(["unknown"], ["item-a"])


def test_revalidates_files_before_loading(tmp_path):
    managed, root, _ = _bundle(tmp_path)
    validated = validate_bundle(managed, root)
    (root / "ranker.json").write_bytes(b"changed after validation")
    with pytest.raises(ControlledLoadError) as caught:
        load_runtime_bundle(validated)
    assert caught.value.code == "bundle_changed"


def test_enforces_resource_limits_before_deserialization(tmp_path):
    managed, root, _ = _bundle(tmp_path)
    validated = validate_bundle(managed, root)
    with pytest.raises(ControlledLoadError) as caught:
        load_runtime_bundle(validated, LoadLimits(max_items=1))
    assert caught.value.code == "resource_limit"
    with pytest.raises(ControlledLoadError) as caught:
        load_runtime_bundle(validated, LoadLimits(max_total_bytes=1))
    assert caught.value.code == "resource_limit"
    with pytest.raises(ControlledLoadError) as caught:
        load_runtime_bundle(validated, LoadLimits(max_json_bytes=1))
    assert caught.value.code == "resource_limit"


def test_rejects_executable_or_unknown_component_format(tmp_path):
    managed, root, manifest = _bundle(tmp_path)
    entry = next(value for value in manifest["files"] if value["role"] == "ranker")
    old_path = root / entry["path"]
    entry["path"] = "ranker.pkl"
    old_path.rename(root / entry["path"])
    _write_manifest(root, manifest)
    with pytest.raises(ControlledLoadError) as caught:
        load_runtime_bundle(validate_bundle(managed, root))
    assert caught.value.code == "unsupported_format"


def test_rejects_component_identity_and_shape_mismatches(tmp_path):
    managed, root, manifest = _bundle(tmp_path)
    encoder = _document(root, manifest, "content_encoder")
    encoder["model_id"] = "wrong-encoder"
    _replace(root, manifest, "content_encoder", _json(encoder))
    with pytest.raises(ControlledLoadError) as caught:
        load_runtime_bundle(validate_bundle(managed, root))
    assert caught.value.code == "component_mismatch"

    managed, root, manifest = _bundle(tmp_path / "schema")
    ranker = _document(root, manifest, "ranker")
    ranker["schema_version"] = True
    _replace(root, manifest, "ranker", _json(ranker))
    with pytest.raises(ControlledLoadError) as caught:
        load_runtime_bundle(validate_bundle(managed, root))
    assert caught.value.code == "component_schema"

    managed, root, manifest = _bundle(tmp_path / "index")
    index = _document(root, manifest, "vector_index")
    index["internal_ids"] = [1, 0]
    _replace(root, manifest, "vector_index", _json(index))
    with pytest.raises(ControlledLoadError) as caught:
        load_runtime_bundle(validate_bundle(managed, root))
    assert caught.value.code == "component_mismatch"


def test_rejects_non_finite_or_unnormalized_embeddings(tmp_path):
    managed, root, manifest = _bundle(tmp_path)
    values = struct.pack("<4f", float("nan"), 0.0, 0.0, 1.0)
    _replace(root, manifest, "item_embeddings", values)
    with pytest.raises(ControlledLoadError) as caught:
        load_runtime_bundle(validate_bundle(managed, root))
    assert caught.value.code == "non_finite"

    managed, root, manifest = _bundle(tmp_path / "norm")
    values = struct.pack("<4f", 2.0, 0.0, 0.0, 1.0)
    _replace(root, manifest, "item_embeddings", values)
    with pytest.raises(ControlledLoadError) as caught:
        load_runtime_bundle(validate_bundle(managed, root))
    assert caught.value.code == "component_mismatch"


def test_rejects_golden_sample_drift(tmp_path):
    managed, root, manifest = _bundle(tmp_path)
    ranker = _document(root, manifest, "ranker")
    ranker["validation_samples"][0]["expected_scores"][0] = 0.5
    _replace(root, manifest, "ranker", _json(ranker))
    with pytest.raises(ControlledLoadError) as caught:
        load_runtime_bundle(validate_bundle(managed, root))
    assert caught.value.code == "sample_mismatch"

    managed, root, manifest = _bundle(tmp_path / "unknown")
    ranker = _document(root, manifest, "ranker")
    ranker["validation_samples"][0]["candidate_item_ids"][0] = "unknown"
    _replace(root, manifest, "ranker", _json(ranker))
    with pytest.raises(ControlledLoadError) as caught:
        load_runtime_bundle(validate_bundle(managed, root))
    assert caught.value.code == "sample_mismatch"


def test_load_command_is_machine_readable_and_does_not_activate(tmp_path, capsys):
    managed, root, _ = _bundle(tmp_path)
    assert main([str(managed), str(root)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "passed"
    assert result["validation_samples_checked"] == 2
    assert result["activated"] is False
