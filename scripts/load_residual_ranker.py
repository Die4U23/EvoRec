"""Validate the R06 ranker component without registering or activating a model."""

import argparse
import json
from pathlib import Path

from evorec.infrastructure.model_runtime import ControlledLoadError
from evorec.infrastructure.residual_ranker import load_residual_ranker


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component_dir", type=Path)
    parser.add_argument("--expected-manifest-sha256")
    args = parser.parse_args(argv)
    try:
        runtime = load_residual_ranker(args.component_dir,
                                       expected_manifest_sha256=args.expected_manifest_sha256)
    except ControlledLoadError as error:
        print(json.dumps({"status": "failed", "code": error.code, "message": str(error)}))
        return 1
    print(json.dumps({"status": "passed", "component_only": True, "activated": False,
                      "dimension": runtime.dimension, "manifest_sha256": runtime.manifest_sha256,
                      "selected_method": runtime.provenance["selected_method"],
                      "validation_samples_checked": runtime.validation_samples_checked}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
