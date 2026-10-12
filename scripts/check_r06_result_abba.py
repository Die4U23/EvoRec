"""Check preregistered old/new untraced evidence; never send requests or prove SLA."""

import argparse
import ast
import json
from pathlib import Path
from subprocess import CalledProcessError

from evorec.infrastructure.model_runtime import ControlledLoadError
from scripts.assemble_r06_bundle import _source
from scripts.check_r06_trace_abba import EvidenceError, check_arm, require
from scripts.run_r06_demo import marker
from scripts.verify_r06_reliability import subprocess_sources


CONTROL = "2f50e6226380eba5e3a7c382dedb58b44ad7aeba"
ORDER = (("a1", "A"), ("b1", "B"), ("b2", "B"), ("a2", "A"))
RELATIVE = Path("artifacts/diagnostics/stable-result-abba-20261011")
PROFILE = "scripts/profile_r06_tcp.py"
SELF = "scripts/check_r06_result_abba.py"
CANDIDATE_FILES = {
    "src/evorec/infrastructure/postgres.py": "334f74df224c7a91eb352d1e18f52e812e054e30ef356e2e3596e4d6ddeba199",
    "src/evorec/infrastructure/recommendation_execution.py": "0f6b2ffea2e5b666b9e353624eda85457817954ccc0e4a06e1b4363887e94bd6",
}


def check_source_pair(control, candidate, old, new):
    """Only the registered production delta and inactive trace helper may differ."""
    production = lambda hashes: {k: v for k, v in hashes.items()
                                 if k.startswith(("src/", "db/migrations/"))}
    before, after = production(old), production(new)
    require(bool(before) and before.keys() == after.keys(), "production file set")
    changed = {name for name in before if before[name] != after[name]}
    require(changed == CANDIDATE_FILES.keys()
            and all(after.get(name) == digest for name, digest in CANDIDATE_FILES.items()), "production delta")
    scripts = {name for name in old.keys() | new.keys() if name.startswith("scripts/")}
    require({name for name in scripts if old.get(name) != new.get(name)} == {PROFILE, SELF}
            and SELF not in old and SELF in new, "diagnostic harness delta")

    def untraced_ast(project):
        tree = ast.parse((project / PROFILE).read_bytes())
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "_fresh_database_trace_complete"]
        require(len(helpers) == 1, "trace helper identity")
        tree.body.remove(helpers[0])
        return ast.dump(tree, include_attributes=False)

    require(untraced_ast(control) == untraced_ast(candidate), "untraced harness changed")


def arm(name, control_root, candidate_root, candidate_commit, old, new):
    variant = dict(ORDER)[name]
    directory, commit, hashes = ((control_root / name, CONTROL, old) if variant == "A"
                                 else (candidate_root / name, candidate_commit, new))
    return dict(arm=name, variant=variant, **check_arm(directory, commit, hashes, False))


def check_series(control_root, candidate_root, candidate_commit, old, new):
    arms = [arm(name, control_root, candidate_root, candidate_commit, old, new) for name, _ in ORDER]
    require(len({row["run_id"] for row in arms}) == 4
            and len({row["schema"] for row in arms}) == 4, "reused ownership")
    return dict(status="old_new_diagnostic_checked_not_performance_acceptance",
                order=[name for name, _ in ORDER], control_commit=CONTROL,
                candidate_commit=candidate_commit, arms=arms,
                zero_failures=all(row["zero_failures"] for row in arms),
                live_resource_checks_performed=False, production_acceptance=False, sla_proven=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("control_project", type=Path)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--arm", choices=[name for name, _ in ORDER])
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args(argv)
    try:
        require(not (args.arm and args.save), "save requires complete series")
        candidate = Path(__file__).resolve().parents[1]
        control = args.control_project.resolve(strict=True)
        require(control != candidate and _source(control) == CONTROL
                and _source(candidate) == args.candidate_commit
                and args.candidate_commit != CONTROL, "clean source identity")
        old, new = subprocess_sources(control), subprocess_sources(candidate)
        check_source_pair(control, candidate, old, new)
        roots = control / RELATIVE, candidate / RELATIVE
        result = (arm(args.arm, *roots, args.candidate_commit, old, new) if args.arm
                  else check_series(*roots, args.candidate_commit, old, new))
        require(_source(control) == CONTROL and _source(candidate) == args.candidate_commit
                and subprocess_sources(control) == old and subprocess_sources(candidate) == new,
                "source changed during check")
        if args.save:
            marker(roots[1], "series-check", result)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, SyntaxError,
            ControlledLoadError, CalledProcessError) as error:
        print(json.dumps(dict(status="old_new_diagnostic_rejected",
                              failed_check=str(error) if type(error) is EvidenceError else type(error).__name__)))
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
