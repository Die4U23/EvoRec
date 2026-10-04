"""Validate an approved text encoder and optional frozen features; no activation."""

import argparse
import json
from pathlib import Path

from evorec.infrastructure.content_encoder import load_content_encoder
from evorec.infrastructure.r06_features import load_r06_features
from evorec.infrastructure.residual_ranker import ControlledLoadError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component_dir", type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--features-component", type=Path)
    parser.add_argument("--expected-features-manifest-sha256")
    args = parser.parse_args(argv)
    if (args.features_component is None) != (args.expected_features_manifest_sha256 is None):
        parser.error("feature component and approved feature digest must be supplied together")
    try:
        runtime = load_content_encoder(args.component_dir, expected_manifest_sha256=args.expected_manifest_sha256)
        if args.features_component is not None:
            runtime.check_features(load_r06_features(
                args.features_component, expected_manifest_sha256=args.expected_features_manifest_sha256))
    except ControlledLoadError as error:
        print(json.dumps({"status": "failed", "code": error.code, "message": str(error)}))
        return 1
    print(json.dumps({"status": "passed", "component_only": True, "activated": False,
                      "manifest_sha256": runtime.manifest_sha256, "dimension": runtime.dimension,
                      "vocabulary_terms": runtime.vocabulary_terms,
                      "validation_samples_checked": runtime.validation_samples_checked,
                      "feature_binding_checked": args.features_component is not None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
