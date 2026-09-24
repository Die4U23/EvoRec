"""Bundle validation covers trust-boundary failures without loading model code."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from evorec.infrastructure.bundle import BundleValidationError, validate_bundle
from scripts.validate_bundle import main


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


def _write_bundle(tmp_path, *, item_ids=("item-a", "item-b"), dimension=3):
    managed = tmp_path / "managed"
    bundle_id = str(uuid4())
    bundle = managed / bundle_id
    bundle.mkdir(parents=True)
    mapping = [{"internal_id": index, "item_id": item_id}
               for index, item_id in enumerate(item_ids)]
    mapping_bytes = _json_bytes(mapping)
    embedding_bytes = b"\0" * (len(item_ids) * dimension * 4)
    contents = {
        "item_mapping": ("items.json", mapping_bytes),
        "item_embeddings": ("embeddings.f32", embedding_bytes),
        "ranker": ("ranker.safetensors", b"opaque ranker"),
        "content_encoder": ("encoder.safetensors", b"opaque encoder"),
        "semantic_codebook": ("codebook.bin", b"opaque codebook"),
        "vector_index": ("index.bin", b"opaque index"),
    }
    for path, content in contents.values():
        (bundle / path).write_bytes(content)
    manifest = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "created_at": "2026-09-23T10:00:00+08:00",
        "source": {
            "build_task_id": "build-001",
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
            "item_count": len(item_ids),
            "item_set_sha256": _sha256(_json_bytes(list(item_ids))),
        },
        "index": {
            "kind": "flat",
            "dimension": dimension,
            "dtype": "float32",
            "normalized": True,
            "distance": "cosine",
            "parameters": {},
        },
        "strategy": {
            "supported_paths": ["content", "collaborative"],
            "retrieval_budget": 400,
            "ranking_budget": 200,
        },
        "files": [
            {"role": role, "path": path, "size_bytes": len(content),
             "sha256": _sha256(content)}
            for role, (path, content) in contents.items()
        ],
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return managed, bundle, manifest


def _rewrite_manifest(bundle: Path, manifest):
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _replace_file(bundle, manifest, role, content):
    row = next(value for value in manifest["files"] if value["role"] == role)
    (bundle / row["path"]).write_bytes(content)
    row["size_bytes"] = len(content)
    row["sha256"] = _sha256(content)
    _rewrite_manifest(bundle, manifest)


def test_valid_bundle_returns_verified_identity(tmp_path):
    managed, bundle, _ = _write_bundle(tmp_path)
    result = validate_bundle(managed, bundle)
    assert result.bundle_id == bundle.name
    assert result.item_count == 2
    assert result.embedding_dimension == 3
    assert result.embedding_dtype == "float32"
    assert len(result.manifest_sha256) == 64
    assert {value.role for value in result.files} == {
        "item_mapping", "item_embeddings", "ranker", "content_encoder",
        "semantic_codebook", "vector_index",
    }


def test_rejects_path_traversal_before_opening_file(tmp_path):
    managed, bundle, manifest = _write_bundle(tmp_path)
    manifest["files"][0]["path"] = "../outside.json"
    _rewrite_manifest(bundle, manifest)
    with pytest.raises(BundleValidationError) as caught:
        validate_bundle(managed, bundle)
    assert caught.value.code == "unsafe_path"


def test_rejects_wrong_hash_and_unlisted_file(tmp_path):
    managed, bundle, manifest = _write_bundle(tmp_path)
    manifest["files"][0]["sha256"] = "c" * 64
    _rewrite_manifest(bundle, manifest)
    with pytest.raises(BundleValidationError) as caught:
        validate_bundle(managed, bundle)
    assert caught.value.code == "hash_mismatch"

    managed, bundle, _ = _write_bundle(tmp_path / "second")
    (bundle / "forgotten.bin").write_bytes(b"not declared")
    with pytest.raises(BundleValidationError) as caught:
        validate_bundle(managed, bundle)
    assert caught.value.code == "unlisted_file"


def test_rejects_duplicate_items_and_noncontiguous_internal_ids(tmp_path):
    managed, bundle, manifest = _write_bundle(tmp_path)
    duplicate = _json_bytes([
        {"internal_id": 0, "item_id": "same"},
        {"internal_id": 1, "item_id": "same"},
    ])
    _replace_file(bundle, manifest, "item_mapping", duplicate)
    with pytest.raises(BundleValidationError) as caught:
        validate_bundle(managed, bundle)
    assert caught.value.code == "mapping_invalid"

    managed, bundle, manifest = _write_bundle(tmp_path / "second")
    skipped = _json_bytes([
        {"internal_id": 0, "item_id": "item-a"},
        {"internal_id": 2, "item_id": "item-b"},
    ])
    _replace_file(bundle, manifest, "item_mapping", skipped)
    with pytest.raises(BundleValidationError) as caught:
        validate_bundle(managed, bundle)
    assert caught.value.code == "mapping_invalid"


def test_rejects_embedding_shape_even_when_hash_and_size_match(tmp_path):
    managed, bundle, manifest = _write_bundle(tmp_path)
    _replace_file(bundle, manifest, "item_embeddings", b"\0" * 20)
    with pytest.raises(BundleValidationError) as caught:
        validate_bundle(managed, bundle)
    assert caught.value.code == "incompatible_artifacts"


def test_rejects_unknown_fields_and_inconsistent_budgets(tmp_path):
    managed, bundle, manifest = _write_bundle(tmp_path)
    manifest["unexpected"] = True
    _rewrite_manifest(bundle, manifest)
    with pytest.raises(BundleValidationError) as caught:
        validate_bundle(managed, bundle)
    assert caught.value.code == "manifest_schema"

    managed, bundle, manifest = _write_bundle(tmp_path / "second")
    manifest["strategy"]["ranking_budget"] = 401
    _rewrite_manifest(bundle, manifest)
    with pytest.raises(BundleValidationError) as caught:
        validate_bundle(managed, bundle)
    assert caught.value.code == "incompatible_metadata"

    managed, bundle, manifest = _write_bundle(tmp_path / "third")
    manifest["artifacts"]["ranker_role"] = "missing_ranker"
    _rewrite_manifest(bundle, manifest)
    with pytest.raises(BundleValidationError) as caught:
        validate_bundle(managed, bundle)
    assert caught.value.code == "incompatible_artifacts"


def test_rejects_bundle_outside_managed_root(tmp_path):
    managed, _, _ = _write_bundle(tmp_path)
    _, outside, _ = _write_bundle(tmp_path / "outside")
    with pytest.raises(BundleValidationError) as caught:
        validate_bundle(managed, outside)
    assert caught.value.code == "unsafe_path"


def test_command_reports_machine_readable_success_and_failure(tmp_path, capsys):
    managed, bundle, manifest = _write_bundle(tmp_path)
    assert main([str(managed), str(bundle)]) == 0
    success = json.loads(capsys.readouterr().out)
    assert success["status"] == "passed"
    assert success["bundle_id"] == bundle.name

    manifest["files"][0]["sha256"] = "c" * 64
    _rewrite_manifest(bundle, manifest)
    assert main([str(managed), str(bundle)]) == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure["status"] == "failed"
    assert failure["code"] == "hash_mismatch"
