"""Assemble approved frozen R06 bytes from a clean commit; never activate them."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from uuid import UUID

from evorec.infrastructure.r06_bundle import FILES, assemble_r06_bundle
from evorec.infrastructure.residual_ranker import ControlledLoadError


def _source(project):
    revision = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    if subprocess.check_output(["git", "-C", str(project), "status", "--porcelain"], text=True).strip():
        raise ControlledLoadError("source_dirty", "commit source before recorded assembly")
    return revision


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("managed_root", type=Path)
    parser.add_argument("--bundle-id", type=UUID, required=True)
    for role in FILES:
        parser.add_argument(f"--{role}-component", type=Path, required=True)
        parser.add_argument(f"--expected-{role}-manifest-sha256", required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args(argv)
    project = Path(__file__).resolve().parents[1]
    digest, target = None, None
    try:
        root = args.managed_root.resolve()
        if not root.is_relative_to(project / "artifacts") or root == project / "artifacts":
            raise ControlledLoadError("unsafe_path", "assembly root must be an artifacts subdirectory")
        revision = _source(project)
        root.mkdir(parents=True, exist_ok=True)
        target = root / str(args.bundle_id)
        digest = assemble_r06_bundle(root, args.bundle_id,
            {role: getattr(args, f"{role}_component") for role in FILES},
            {role: getattr(args, f"expected_{role}_manifest_sha256") for role in FILES},
            args.catalog, args.metadata, assembly_revision=revision)
        if _source(project) != revision:
            raise ControlledLoadError("source_changed", "source commit changed during assembly")
    except BaseException as error:
        # Withdraw only our own new commit marker, never an existing/rival package.
        if digest and target:
            try:
                if hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest() == digest:
                    (target / "manifest.json").unlink()
            except FileNotFoundError:
                pass
        if not isinstance(error, (ValueError, OSError, subprocess.SubprocessError)):
            raise
        print(json.dumps(dict(status="failed", code=getattr(error, "code", "assembly_failed"), message=str(error))))
        return 1
    print(json.dumps(dict(status="passed", activated=False, bundle_id=str(args.bundle_id),
                         manifest_sha256=digest, assembly_revision=revision, working_tree_dirty=False)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
