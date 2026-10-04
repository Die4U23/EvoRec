"""Validate a hash-approved R06 package without changing the active model."""

import argparse
import json
from pathlib import Path

from evorec.infrastructure.r06_bundle import load_r06_bundle
from evorec.infrastructure.residual_ranker import ControlledLoadError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("managed_root", type=Path)
    parser.add_argument("bundle_dir", type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--content-backend", choices=("stdlib", "numpy"), default="stdlib")
    args = parser.parse_args(argv)
    try:
        runtime = load_r06_bundle(args.managed_root, args.bundle_dir,
                                 expected_manifest_sha256=args.expected_manifest_sha256,
                                 content_backend=args.content_backend)
    except ControlledLoadError as error:
        print(json.dumps(dict(status="failed", code=error.code, message=str(error))))
        return 1
    print(json.dumps(dict(status="passed", activated=False, bundle_id=str(runtime.bundle_id),
                         manifest_sha256=runtime.manifest_sha256, model_version=runtime.model_version,
                         item_count=len(runtime.catalog_item_sha256), content_backend=args.content_backend)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
