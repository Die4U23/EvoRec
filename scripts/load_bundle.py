"""Validate and controlled-load one bundle without activating it."""

import argparse
import json
from pathlib import Path

from evorec.infrastructure.bundle import BundleValidationError, validate_bundle
from evorec.infrastructure.model_runtime import ControlledLoadError, load_runtime_bundle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("managed_root", type=Path)
    parser.add_argument("bundle_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        validated = validate_bundle(args.managed_root, args.bundle_dir)
        runtime = load_runtime_bundle(validated)
    except (BundleValidationError, ControlledLoadError) as error:
        print(json.dumps({"status": "failed", "code": error.code, "message": str(error)}))
        return 1
    print(json.dumps({
        "status": "passed",
        "bundle_id": runtime.bundle_id,
        "manifest_sha256": runtime.manifest_sha256,
        "item_count": len(runtime.item_ids),
        "dimension": runtime.dimension,
        "model_id": runtime.model_id,
        "validation_samples_checked": runtime.validation_samples_checked,
        "activated": False,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
