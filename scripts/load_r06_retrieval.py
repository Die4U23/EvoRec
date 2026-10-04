"""Replay approved frozen retrieval/features and optional ranker; no activation."""

import argparse
import json
from pathlib import Path

from evorec.infrastructure.r06_features import load_r06_features
from evorec.infrastructure.r06_retrieval import load_r06_retrieval
from evorec.infrastructure.residual_ranker import ControlledLoadError, load_residual_ranker


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component_dir", type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--features-component", type=Path, required=True)
    parser.add_argument("--expected-features-manifest-sha256", required=True)
    parser.add_argument("--ranker-component", type=Path)
    parser.add_argument("--expected-ranker-manifest-sha256")
    args = parser.parse_args(argv)
    if bool(args.ranker_component) != bool(args.expected_ranker_manifest_sha256):
        parser.error("ranker component and approved digest must be supplied together")
    try:
        features = load_r06_features(args.features_component, expected_manifest_sha256=args.expected_features_manifest_sha256)
        runtime = load_r06_retrieval(args.component_dir, features, expected_manifest_sha256=args.expected_manifest_sha256)
        if args.ranker_component:
            ranker = load_residual_ranker(args.ranker_component, expected_manifest_sha256=args.expected_ranker_manifest_sha256)
            # No catalog scan: empty request also verifies all ranker bindings.
            features.build_pool([], [], 0, [], []).score(ranker)
    except ControlledLoadError as error:
        print(json.dumps({"status": "failed", "code": error.code, "message": str(error)}))
        return 1
    print(json.dumps({"status": "passed", "component_only": True, "activated": False,
                      "manifest_sha256": runtime.manifest_sha256, "edge_count": runtime.edge_count,
                      "validation_samples_checked": runtime.validation_samples_checked,
                      "ranker_binding_checked": args.ranker_component is not None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
