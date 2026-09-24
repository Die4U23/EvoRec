"""Validate one immutable bundle candidate beneath a managed artifact root."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from evorec.infrastructure.bundle import BundleValidationError, validate_bundle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("managed_root", type=Path)
    parser.add_argument("bundle_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        result = validate_bundle(args.managed_root, args.bundle_dir)
    except BundleValidationError as error:
        print(json.dumps({"status": "failed", "code": error.code, "message": str(error)}))
        return 1
    payload = asdict(result)
    payload["root"] = str(payload["root"])
    print(json.dumps({"status": "passed", **payload}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
