"""Validate immutable bundle candidates without deserializing executable models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
from uuid import UUID


SHA256 = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
DTYPE_BYTES = {"float16": 2, "float32": 4}


class BundleValidationError(ValueError):
    """A stable validation failure suitable for CLI and service boundaries."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BundleFile:
    role: str
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ValidatedBundle:
    bundle_id: str
    root: Path
    manifest_sha256: str
    item_count: int
    embedding_dimension: int
    embedding_dtype: str
    files: tuple[BundleFile, ...]


def _fail(code: str, message: str) -> None:
    raise BundleValidationError(code, message)


def _object(value: Any, name: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("manifest_schema", f"{name} must be an object")
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        _fail("manifest_schema", f"{name} keys mismatch; missing={missing}, extra={extra}")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("manifest_schema", f"{name} must be a non-empty string")
    return value


def _integer(value: Any, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail("manifest_schema", f"{name} must be an integer >= {minimum}")
    return value


def _sha256(value: Any, name: str) -> str:
    value = _string(value, name)
    if SHA256.fullmatch(value) is None:
        _fail("manifest_schema", f"{name} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: Any, name: str) -> datetime:
    value = _string(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("manifest_schema", f"{name} must be an RFC 3339 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("manifest_schema", f"{name} must include a timezone")
    return parsed


def _safe_relative_path(value: Any, name: str) -> str:
    value = _string(value, name)
    path = PurePosixPath(value)
    if (path.is_absolute() or value != path.as_posix() or "\\" in value
            or any(part in {"", ".", ".."} for part in path.parts)):
        _fail("unsafe_path", f"{name} must be a normalized relative POSIX path")
    return value


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _reject_json_constant(value: str) -> None:
    _fail("manifest_invalid_json", f"non-standard JSON constant is not allowed: {value}")


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for key, item in pairs:
        if key in value:
            _fail("manifest_invalid_json", f"duplicate JSON object key is not allowed: {key}")
        value[key] = item
    return value


def _read_manifest(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        _fail("manifest_unreadable", "manifest.json must be a regular file inside the bundle")
    try:
        raw = path.read_bytes()
    except OSError as error:
        _fail("manifest_unreadable", f"cannot read manifest: {error}")
    try:
        document = json.loads(raw, parse_constant=_reject_json_constant,
                              object_pairs_hook=_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail("manifest_invalid_json", f"manifest is not valid UTF-8 JSON: {error}")
    document = _object(document, "manifest", {
        "schema_version", "bundle_id", "created_at", "source", "data_protocol",
        "artifacts", "catalog", "index", "strategy", "files",
    })
    return document, hashlib.sha256(raw).hexdigest()


def _validate_identity(document: dict[str, Any]) -> tuple[str, datetime, datetime]:
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        _fail("unsupported_schema", "schema_version must be 1")
    bundle_id = _string(document["bundle_id"], "bundle_id")
    try:
        canonical = str(UUID(bundle_id))
    except ValueError:
        _fail("manifest_schema", "bundle_id must be a canonical UUID")
    if canonical != bundle_id:
        _fail("manifest_schema", "bundle_id must be a canonical lowercase UUID")
    created_at = _timestamp(document["created_at"], "created_at")

    source = _object(document["source"], "source", {
        "build_task_id", "code_revision", "code_dirty", "dataset_sha256",
    })
    _string(source["build_task_id"], "source.build_task_id")
    revision = _string(source["code_revision"], "source.code_revision")
    if REVISION.fullmatch(revision) is None:
        _fail("manifest_schema", "source.code_revision must be a lowercase 40-character Git revision")
    if type(source["code_dirty"]) is not bool:
        _fail("manifest_schema", "source.code_dirty must be a boolean")
    _sha256(source["dataset_sha256"], "source.dataset_sha256")

    protocol = _object(document["data_protocol"], "data_protocol", {
        "training_cutoff", "availability_rule", "static_metadata_rule",
    })
    cutoff = _timestamp(protocol["training_cutoff"], "data_protocol.training_cutoff")
    _string(protocol["availability_rule"], "data_protocol.availability_rule")
    _string(protocol["static_metadata_rule"], "data_protocol.static_metadata_rule")
    if cutoff > created_at:
        _fail("incompatible_metadata", "training cutoff cannot be later than bundle creation")
    return bundle_id, created_at, cutoff


def _validate_files(bundle_root: Path, rows: Any) -> tuple[tuple[BundleFile, ...], dict[str, BundleFile]]:
    if not isinstance(rows, list) or not rows:
        _fail("manifest_schema", "files must be a non-empty array")
    files, by_role, paths = [], {}, set()
    for index, value in enumerate(rows):
        row = _object(value, f"files[{index}]", {"role", "path", "size_bytes", "sha256"})
        role = _string(row["role"], f"files[{index}].role")
        relative = _safe_relative_path(row["path"], f"files[{index}].path")
        size = _integer(row["size_bytes"], f"files[{index}].size_bytes", minimum=0)
        digest = _sha256(row["sha256"], f"files[{index}].sha256")
        if role in by_role or relative in paths:
            _fail("duplicate_file", f"duplicate file role or path: {role}, {relative}")
        candidate = bundle_root.joinpath(*PurePosixPath(relative).parts)
        if candidate.is_symlink() or not candidate.is_file():
            _fail("file_missing", f"listed file is missing or is not a regular file: {relative}")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            _fail("file_missing", f"cannot resolve listed file {relative}: {error}")
        if not resolved.is_relative_to(bundle_root):
            _fail("unsafe_path", f"listed file escapes bundle root: {relative}")
        try:
            actual_size = resolved.stat().st_size
            actual_digest = _digest(resolved)
        except OSError as error:
            _fail("file_unreadable", f"cannot read listed file {relative}: {error}")
        if actual_size != size:
            _fail("size_mismatch", f"size mismatch for {relative}: expected {size}, got {actual_size}")
        if actual_digest != digest:
            _fail("hash_mismatch", f"SHA-256 mismatch for {relative}")
        entry = BundleFile(role, relative, size, digest)
        files.append(entry)
        by_role[role] = entry
        paths.add(relative)

    actual_paths = set()
    for candidate in bundle_root.rglob("*"):
        if candidate.is_symlink():
            _fail("unsafe_path", f"bundle contains a symbolic link: {candidate.relative_to(bundle_root)}")
        if candidate.is_file():
            actual_paths.add(candidate.relative_to(bundle_root).as_posix())
    expected_paths = paths | {"manifest.json"}
    if actual_paths != expected_paths:
        _fail("unlisted_file", "bundle file set mismatch; "
              f"unlisted={sorted(actual_paths - expected_paths)}, "
              f"missing={sorted(expected_paths - actual_paths)}")
    return tuple(files), by_role


def _validate_compatibility(document: dict[str, Any], bundle_root: Path,
                            by_role: dict[str, BundleFile]) -> tuple[int, int, str]:
    artifacts = _object(document["artifacts"], "artifacts", {
        "item_mapping_role", "item_embeddings_role", "ranker_role", "ranker_id",
        "content_encoder_role", "content_encoder_id", "semantic_codebook_role",
        "semantic_codebook_id", "vector_index_role",
    })
    role_fields = (
        "item_mapping_role", "item_embeddings_role", "ranker_role",
        "content_encoder_role", "semantic_codebook_role", "vector_index_role",
    )
    artifact_roles = {
        field: _string(artifacts[field], f"artifacts.{field}") for field in role_fields
    }
    for field in ("ranker_id", "content_encoder_id", "semantic_codebook_id"):
        _string(artifacts[field], f"artifacts.{field}")
    missing_roles = sorted(set(artifact_roles.values()) - set(by_role))
    if len(set(artifact_roles.values())) != len(artifact_roles) or missing_roles:
        _fail("incompatible_artifacts",
              f"artifact roles must name distinct listed files; missing={missing_roles}")
    mapping_role = artifact_roles["item_mapping_role"]
    embeddings_role = artifact_roles["item_embeddings_role"]

    catalog = _object(document["catalog"], "catalog", {"item_count", "item_set_sha256"})
    item_count = _integer(catalog["item_count"], "catalog.item_count")
    expected_item_set = _sha256(catalog["item_set_sha256"], "catalog.item_set_sha256")

    index = _object(document["index"], "index", {
        "kind", "dimension", "dtype", "normalized", "distance", "parameters",
    })
    _string(index["kind"], "index.kind")
    dimension = _integer(index["dimension"], "index.dimension")
    dtype = _string(index["dtype"], "index.dtype")
    if dtype not in DTYPE_BYTES:
        _fail("incompatible_artifacts", f"unsupported embedding dtype: {dtype}")
    if type(index["normalized"]) is not bool:
        _fail("manifest_schema", "index.normalized must be a boolean")
    if _string(index["distance"], "index.distance") not in {"cosine", "dot", "l2"}:
        _fail("incompatible_artifacts", "index.distance must be cosine, dot, or l2")
    if not isinstance(index["parameters"], dict):
        _fail("manifest_schema", "index.parameters must be an object")

    strategy = _object(document["strategy"], "strategy", {
        "supported_paths", "retrieval_budget", "ranking_budget",
    })
    supported = strategy["supported_paths"]
    if (not isinstance(supported, list) or not supported
            or any(not isinstance(value, str) or not value for value in supported)
            or len(set(supported)) != len(supported)):
        _fail("manifest_schema", "strategy.supported_paths must contain unique non-empty strings")
    retrieval = _integer(strategy["retrieval_budget"], "strategy.retrieval_budget")
    ranking = _integer(strategy["ranking_budget"], "strategy.ranking_budget")
    if ranking > retrieval:
        _fail("incompatible_metadata", "ranking budget cannot exceed retrieval budget")

    mapping_file = bundle_root.joinpath(*PurePosixPath(by_role[mapping_role].path).parts)
    try:
        mapping = json.loads(mapping_file.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail("mapping_invalid", f"item mapping is not valid UTF-8 JSON: {error}")
    if not isinstance(mapping, list) or len(mapping) != item_count:
        _fail("mapping_invalid", "item mapping length must equal catalog.item_count")
    item_ids = []
    for internal_id, value in enumerate(mapping):
        row = _object(value, f"item_mapping[{internal_id}]", {"internal_id", "item_id"})
        if type(row["internal_id"]) is not int or row["internal_id"] != internal_id:
            _fail("mapping_invalid", "item mapping internal IDs must be contiguous from zero")
        item_ids.append(_string(row["item_id"], f"item_mapping[{internal_id}].item_id"))
    if len(set(item_ids)) != len(item_ids):
        _fail("mapping_invalid", "item mapping contains duplicate external item IDs")
    canonical_items = json.dumps(item_ids, ensure_ascii=False, separators=(",", ":")).encode()
    if hashlib.sha256(canonical_items).hexdigest() != expected_item_set:
        _fail("mapping_invalid", "catalog.item_set_sha256 does not match the ordered item mapping")

    embedding_file = by_role[embeddings_role]
    expected_bytes = item_count * dimension * DTYPE_BYTES[dtype]
    if embedding_file.size_bytes != expected_bytes:
        _fail("incompatible_artifacts", f"embedding byte size must be {expected_bytes}")
    return item_count, dimension, dtype


def validate_bundle(managed_root: Path | str, bundle_dir: Path | str) -> ValidatedBundle:
    """Validate a candidate beneath a managed root and return its immutable identity."""
    try:
        root = Path(managed_root).resolve(strict=True)
        candidate = Path(bundle_dir).resolve(strict=True)
    except OSError as error:
        _fail("bundle_missing", f"managed root or bundle directory is missing: {error}")
    if (not root.is_dir() or not candidate.is_dir() or candidate == root
            or not candidate.is_relative_to(root)):
        _fail("unsafe_path", "bundle directory must be a child directory of the managed root")
    if Path(bundle_dir).is_symlink():
        _fail("unsafe_path", "bundle directory cannot be a symbolic link")

    document, manifest_digest = _read_manifest(candidate / "manifest.json")
    bundle_id, _, _ = _validate_identity(document)
    if candidate.name != bundle_id:
        _fail("bundle_identity_mismatch", "bundle directory name must equal bundle_id")
    files, by_role = _validate_files(candidate, document["files"])
    item_count, dimension, dtype = _validate_compatibility(document, candidate, by_role)
    return ValidatedBundle(bundle_id, candidate, manifest_digest, item_count, dimension, dtype, files)
