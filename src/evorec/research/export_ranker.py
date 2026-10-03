"""Export the archived, validation-selected R06 A-frozen-s17 component on CPU.

Research-only entrypoint: torch.load(weights_only=True) never runs in the service.
It uses validation inputs, not targets/test metrics, and does not retrain/select.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import torch

from evorec.infrastructure.residual_ranker import (
    SCALAR_NAMES as RUNTIME_SCALAR_NAMES, SCORE_TOLERANCE, load_residual_ranker,
)
from evorec.research.content import ContentFeatures
from evorec.research.ranker import ResidualListRanker, SCALAR_NAMES, load_ranker


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _checked(project, record):
    path = (project / record["path_from_project_root"]).resolve()
    if not path.is_relative_to(project) or _sha(path) != record["sha256"]:
        raise ValueError("research artifact path or hash mismatch")
    return path


def _json_bytes(value):
    return json.dumps(value, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _record(name, raw):
    return {"path": name, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def write_component(model, provenance, samples, output):
    """Serialize fixed Linear/GELU/Linear/GELU/Linear weights; never overwrite.

    A failed replay removes the manifest so the partial directory is not loadable.
    Caller must provide reference scores obtained independently from Torch.
    """
    architecture = (torch.nn.Linear, torch.nn.GELU, torch.nn.Linear, torch.nn.GELU, torch.nn.Linear)
    if (type(model) is not ResidualListRanker
            or tuple(type(layer) for layer in model.network) != architecture
            or any(model.network[i].approximate != "none" for i in (1, 3))
            or any(parameter.dtype != torch.float32 for parameter in model.parameters())):
        raise ValueError("only the float32 exact-GELU residual architecture is supported")
    state = model.state_dict()
    names = [f"network.{layer}.{field}" for layer in (0, 2, 4) for field in ("weight", "bias")]
    if set(state) != set(names):
        raise ValueError("checkpoint contains an unsupported architecture")
    raw = b"".join(state[name].detach().cpu().numpy().astype("<f4").tobytes(order="C")
                   for name in names)
    validation = _json_bytes(samples)
    manifest = {
        "schema_version": 1, "kind": "residual-list-mlp-v1", "dtype": "float32-le",
        "dimension": (model.network[0].in_features - len(SCALAR_NAMES)) // 4,
        "hidden": model.network[0].out_features, "bottleneck": model.network[2].out_features,
        "base_scale": model.base_scale, "residual_scale": model.residual_scale,
        "scalar_names": list(SCALAR_NAMES), "provenance": provenance,
        "weights": _record("weights.f32", raw), "validation": _record("validation.json", validation),
    }
    output.mkdir(parents=True, exist_ok=False)
    manifest_path = output / "manifest.json"
    try:
        (output / "weights.f32").write_bytes(raw)
        (output / "validation.json").write_bytes(validation)
        manifest_path.write_bytes(_json_bytes(manifest))
        return load_residual_ranker(output)
    except Exception:
        manifest_path.unlink(missing_ok=True)
        raise


@torch.inference_mode()
def validation_samples(model, contexts, candidates, scalars, valid):
    """Independent float32 Torch reference with the research empty-history rule."""
    contexts = torch.from_numpy(np.asarray(contexts, dtype=np.float32))
    candidates = torch.from_numpy(np.asarray(candidates, dtype=np.float32))
    scalars = torch.from_numpy(np.asarray(scalars, dtype=np.float32))
    valid = torch.from_numpy(np.asarray(valid, dtype=bool))
    model.eval()
    scores = model(contexts, candidates, scalars, valid)
    empty = contexts.norm(dim=1) <= 1e-8
    scores[empty] = model.base_scale * .5 * (scalars[empty, :, 1] + scalars[empty, :, 2])
    result = []
    for row in range(len(contexts)):
        mask = valid[row]
        expected = scores[row, mask].numpy()
        result.append({
            "context": contexts[row].tolist(), "candidates": candidates[row, mask].tolist(),
            "scalars": scalars[row, mask].tolist(), "expected_scores": expected.tolist(),
            "expected_top20": np.argsort(-expected, kind="stable")[:20].tolist(),
        })
    return result


def export(project, output):
    project = Path(project).resolve()
    output = Path(output).resolve()
    if not output.is_relative_to(project / "artifacts") or output == project / "artifacts":
        raise ValueError("export must use a new directory beneath project artifacts/")
    snapshot = output.with_name(output.name + "-source")
    report_path = output.with_name(output.name + "-verification.json")
    if output.exists() or snapshot.exists() or report_path.exists():
        raise FileExistsError("export destination already exists")
    archive_path = project / "docs/experiments/archive/r06-multi-interest-20260917.json"
    series = _read(archive_path)
    config = series["configuration"]
    if (series["status"] != "completed" or series["selected_method"] != "A-frozen-s17"
            or config["stage"] != "R06-multi-interest"
            or tuple(SCALAR_NAMES) != RUNTIME_SCALAR_NAMES):
        raise ValueError("not the completed validation-selected frozen R06 arm")
    source = project / config["source_run"]
    replica = project / config["source_replication"]
    original_path = _checked(project, {"path_from_project_root": str(source / "series.json"),
                                       "sha256": config["source_series_sha256"]})
    replica_path = _checked(project, {"path_from_project_root": str(replica / "series.json"),
                                      "sha256": config["source_replication_sha256"]})
    original, replication = _read(original_path), _read(replica_path)
    if (original["status"] != "completed" or replication["status"] != "completed"
            or original["configuration"]["model"] != config["model"]
            or original["feature_fingerprint"] != series["feature_fingerprint"]):
        raise ValueError("frozen source protocol, model or features differ")
    checkpoint_record = series["frozen_checkpoints"]["17"]
    trial = next(row for row in replication["trials"] if row["seed"] == 17)
    if trial["checkpoint"] != checkpoint_record:
        raise ValueError("selected checkpoint differs from the replication record")
    checkpoint = _checked(project, checkpoint_record)
    encoder_files = original["content_encoder"]["files"]
    paths = {name: _checked(project, {
        "path_from_project_root": str(source / "content-encoder" / name),
        "sha256": encoder_files[name],
    }) for name in ("items.json", "vectors.npy")}
    features = ContentFeatures(_read(paths["items.json"]),
                               np.load(paths["vectors.npy"], allow_pickle=False))
    if features.fingerprint != series["feature_fingerprint"]:
        raise ValueError("frozen content feature fingerprint differs")
    model = load_ranker(checkpoint, original["protocol_id"], features, config["model"], device="cpu")
    pool_record = series["validation_candidates"]["caches"]["A"]
    pool_path = _checked(project, pool_record)
    with np.load(pool_path, allow_pickle=False) as pool:
        # Neither targets nor query_ids are read. Fixed positions, not outcome-selected cases.
        contexts, ids, scalars = pool["contexts"], pool["items"], pool["scalars"]
    represented = np.flatnonzero(np.linalg.norm(contexts, axis=1) > 1e-8)
    empty = np.flatnonzero(np.linalg.norm(contexts, axis=1) <= 1e-8)
    if len(represented) < 3 or not len(empty):
        raise ValueError("validation pool lacks required represented/empty context cases")
    rows = [int(represented[i]) for i in (0, len(represented) // 2, -1)] + [int(empty[0])]
    ids, contexts, scalars = ids[rows], contexts[rows], scalars[rows]
    if (ids.shape[1] != 400 or (ids < 0).any() or (ids > len(features.items)).any()
            or not (ids > 0).any(axis=1).all()):
        raise ValueError("invalid archived R06 candidate pool")
    vectors = np.vstack((np.zeros((1, features.vectors.shape[1]), dtype=np.float32), features.vectors))
    samples = validation_samples(model, contexts, vectors[ids], scalars, ids > 0)
    provenance = {
        "selected_method": series["selected_method"], "selection_protocol_id": series["protocol_id"],
        "training_protocol_id": original["protocol_id"], "selection_series_sha256": _sha(archive_path),
        "source_series_sha256": config["source_series_sha256"],
        "replication_series_sha256": config["source_replication_sha256"],
        "checkpoint_sha256": checkpoint_record["sha256"], "feature_fingerprint": features.fingerprint,
        "validation_pool_sha256": pool_record["sha256"],
        "items_sha256": encoder_files["items.json"], "vectors_sha256": encoder_files["vectors.npy"],
    }
    # A dirty export is not a fresh training experiment. Preserve changed source
    # bytes as well as the base commit, rather than relying on the commit alone.
    code = _snapshot_code(snapshot)
    started = time.perf_counter()
    runtime = write_component(model, provenance, samples, output)
    load_seconds = time.perf_counter() - started
    maximum_error = 0.
    for sample in samples:
        actual = runtime.score(sample["context"], sample["candidates"], sample["scalars"])
        maximum_error = max(maximum_error, max(abs(a - b) for a, b in zip(
            actual, sample["expected_scores"], strict=True)))
    result = {
        "status": "passed", "component_only": True, "activated": False,
        "manifest_sha256": runtime.manifest_sha256, "provenance": provenance,
        "validation_rows": rows, "validation_samples": len(samples),
        "candidate_counts": [len(sample["candidates"]) for sample in samples],
        "max_absolute_score_error": maximum_error, "score_tolerance": SCORE_TOLERANCE,
        "top20_exact": True, "export_and_controlled_load_seconds": load_seconds,
        "test_split_read": False, "retrained": False,
        "export_code": code,
    }
    report_path.write_bytes(_json_bytes(result))
    return result


def _snapshot_code(destination):
    code_root = Path(__file__).resolve().parents[3]
    paths = (
        "src/evorec/research/export_ranker.py", "src/evorec/research/ranker.py",
        "src/evorec/research/content.py", "src/evorec/infrastructure/residual_ranker.py",
        "src/evorec/infrastructure/model_runtime.py", "src/evorec/infrastructure/bundle.py",
    )
    destination.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for relative in paths:
        raw = (code_root / relative).read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        hashes[relative] = hashlib.sha256(raw).hexdigest()
    return {
        "base_commit": subprocess.check_output(["git", "-C", str(code_root), "rev-parse", "HEAD"], text=True).strip(),
        "working_tree_dirty": bool(subprocess.check_output(
            ["git", "-C", str(code_root), "status", "--porcelain"], text=True).strip()),
        "source_sha256": hashes, "python_version": sys.version.split()[0],
        "torch_version": str(torch.__version__), "numpy_version": np.__version__,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(export(args.project_root, args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
