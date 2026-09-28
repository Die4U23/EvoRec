"""Reproducible local content baseline; no learned R06 weights are used."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import subprocess
from tempfile import TemporaryDirectory
from typing import Callable
from uuid import UUID

from evorec.domain.errors import ManagementError
from evorec.infrastructure.bundle import validate_bundle
from evorec.infrastructure.model_runtime import load_runtime_bundle

DIMENSION = 128
MAX_CATALOG_ITEMS = 5000
ENCODER_ID = "hash-text-title-category-v1"


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_vector(item: dict) -> tuple[float, ...]:
    """Stable nonnegative feature hashing of words and Chinese characters/bigrams."""
    title = item["title"].casefold()
    tokens = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", title)
    tokens += [title[i:i + 2] for i in range(len(title) - 1)
               if all('\u3400' <= char <= '\u9fff' for char in title[i:i + 2])]
    features = Counter("title:" + token for token in tokens)
    features["category:" + item["category"].casefold().strip()] += 3
    vector = [0.0] * DIMENSION
    for feature, count in features.items():
        bucket = int.from_bytes(hashlib.sha256(feature.encode()).digest()[:4], "little") % DIMENSION
        vector[bucket] += count
    norm = math.sqrt(sum(value * value for value in vector))
    # Store and validate exactly the same float32 values used by the runtime.
    return struct.unpack(f"<{DIMENSION}f", struct.pack(
        f"<{DIMENSION}f", *(value / norm for value in vector)))


def content_code(item: dict, vector: tuple[float, ...]) -> str:
    # Content buckets may collide; the item digest is a deterministic unique suffix.
    buckets = sorted(range(DIMENSION), key=lambda index: (-vector[index], index))[:3]
    return ".".join(map(str, buckets)) + ":" + _sha(item["item_id"].encode())


def _provenance() -> tuple[str, bool]:
    root = Path(__file__).resolve().parents[3]
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root, check=True,
                                capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManagementError("build_provenance_unavailable", "Git build provenance is unavailable", 503) from exc
    return revision, bool(status.strip())


def build_catalog_bundle(managed_root: Path, bundle_id: UUID, build_id: UUID,
                         items: list[dict], progress: Callable[[int], None]) -> None:
    if not 1 <= len(items) <= MAX_CATALOG_ITEMS:
        raise ManagementError("catalog_capacity_exceeded", "local content catalog supports 1 to 5000 items", 422)
    managed_root.mkdir(parents=True, exist_ok=True)
    root = managed_root.resolve(strict=True)
    target = root / str(bundle_id)
    if target.exists():
        raise ManagementError("bundle_already_exists", "build artifact already exists")
    revision, dirty = _provenance()
    vectors = []
    for count, item in enumerate(items, 1):
        vectors.append(content_vector(item))
        if count % 100 == 0 or count == len(items):
            progress(count)
    ids = [item["item_id"] for item in items]
    samples = []
    for history_index in range(min(2, len(items))):
        candidate_indices = list(range(min(8, len(items))))
        history = vectors[history_index]
        norm = math.sqrt(sum(value * value for value in history))
        scores = [sum(a / norm * b for a, b in zip(history, vectors[index], strict=True))
                  for index in candidate_indices]
        candidates = [ids[index] for index in candidate_indices]
        samples.append({"history_item_ids": [ids[history_index]],
                        "candidate_item_ids": candidates, "expected_scores": scores,
                        "expected_order": [item for _, item in sorted(
                            zip(scores, candidates, strict=True), key=lambda pair: -pair[0])]})
    infrastructure = Path(__file__).resolve().parent
    contents = {
        "item_mapping": ("items.json", _json([{"internal_id": i, "item_id": item}
                                              for i, item in enumerate(ids)])),
        "item_embeddings": ("embeddings.f32", b"".join(struct.pack(f"<{DIMENSION}f", *v) for v in vectors)),
        "content_encoder": ("encoder.json", _json({
            "schema_version": 1, "kind": "hash-text-mean-history-v1", "model_id": ENCODER_ID,
            "dimension": DIMENSION, "dtype": "float32", "output_normalized": True})),
        "semantic_codebook": ("codes.json", _json({
            "schema_version": 1, "kind": "item-codebook-v1", "codebook_id": "content-buckets-v1",
            "item_count": len(items), "codes": [content_code(item, vector)
                for item, vector in zip(items, vectors, strict=True)]})),
        "vector_index": ("index.json", _json({
            "schema_version": 1, "kind": "flat-v1", "item_count": len(items),
            "dimension": DIMENSION, "dtype": "float32", "normalized": True,
            "distance": "cosine", "internal_ids": list(range(len(items)))})),
        "ranker": ("ranker.json", _json({
            "schema_version": 1, "kind": "dot-product-v1", "model_id": "content-cosine-v1",
            "dimension": DIMENSION, "scale": 1.0, "biases": [0.0] * len(items),
            "validation_samples": samples})),
        "catalog_snapshot": ("catalog.json", _json(items)),
    }
    for role, source in {
        "builder_source": infrastructure / "catalog_builder.py",
        "build_service_source": infrastructure / "catalog_build.py",
        "validator_source": infrastructure / "bundle.py",
        "runtime_source": infrastructure / "model_runtime.py",
        "manager_source": infrastructure / "management.py",
        "recommendation_source": infrastructure / "postgres.py",
    }.items():
        contents[role] = (role.replace("_", "-") + ".txt", source.read_bytes())
    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": 1, "bundle_id": str(bundle_id), "created_at": now,
        "source": {"build_task_id": str(build_id), "code_revision": revision,
                   "code_dirty": dirty, "dataset_sha256": _sha(_json(items))},
        "data_protocol": {"training_cutoff": now,
                          "availability_rule": "explicit catalog publication before admission",
                          "static_metadata_rule": "title and category only; untrained hashing baseline"},
        "artifacts": {"item_mapping_role": "item_mapping", "item_embeddings_role": "item_embeddings",
                      "ranker_role": "ranker", "ranker_id": "content-cosine-v1",
                      "content_encoder_role": "content_encoder", "content_encoder_id": ENCODER_ID,
                      "semantic_codebook_role": "semantic_codebook", "semantic_codebook_id": "content-buckets-v1",
                      "vector_index_role": "vector_index"},
        "catalog": {"item_count": len(items), "item_set_sha256": _sha(_json(ids))},
        "index": {"kind": "flat", "dimension": DIMENSION, "dtype": "float32",
                  "normalized": True, "distance": "cosine", "parameters": {}},
        "strategy": {"supported_paths": ["content"], "retrieval_budget": len(items),
                     "ranking_budget": len(items)},
        "files": [{"role": role, "path": name, "size_bytes": len(data), "sha256": _sha(data)}
                  for role, (name, data) in contents.items()],
    }
    # Only a completely validated directory becomes visible at the final UUID path.
    with TemporaryDirectory(prefix=".catalog-build-", dir=root) as staging:
        candidate = Path(staging) / str(bundle_id)
        candidate.mkdir()
        for name, data in contents.values():
            (candidate / name).write_bytes(data)
        (candidate / "manifest.json").write_bytes(_json(manifest))
        load_runtime_bundle(validate_bundle(root, candidate))
        candidate.rename(target)
