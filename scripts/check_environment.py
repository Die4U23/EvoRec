"""Verify one EvoRec environment profile without changing the machine."""

import argparse
import importlib
import json
import os
import platform
import sys
from importlib.metadata import PackageNotFoundError, version


PROFILES = {
    "service": ("fastapi", "httpx", "pydantic", "pytest", "uvicorn"),
    "database": ("psycopg", "psycopg-binary", "psycopg-pool"),
    "research": ("matplotlib", "numpy", "scikit-learn", "torch"),
}


def package_versions(profile: str) -> tuple[dict[str, str], list[str]]:
    installed: dict[str, str] = {}
    missing: list[str] = []
    for package in PROFILES[profile]:
        try:
            installed[package] = version(package)
        except PackageNotFoundError:
            missing.append(package)
    return installed, missing


def check(profile: str, require_cuda: bool = False) -> dict[str, object]:
    installed, missing = package_versions(profile)
    report: dict[str, object] = {
        "profile": profile,
        "python": platform.python_version(),
        "executable": sys.executable,
        "packages": installed,
        "missing": missing,
    }
    if profile == "database" and not missing:
        psycopg = importlib.import_module("psycopg")
        report["psycopg_implementation"] = psycopg.pq.__impl__
        report["database_url_configured"] = bool(os.getenv("EVOREC_DATABASE_URL"))
    if profile == "research" and not missing:
        torch = importlib.import_module("torch")
        cuda_available = torch.cuda.is_available()
        report.update({
            "cuda_available": cuda_available,
            "cuda_version": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(0) if cuda_available else None,
        })
        if require_cuda and not cuda_available:
            report["missing"] = ["cuda-runtime"]
    report["ok"] = not report["missing"]
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=PROFILES)
    parser.add_argument("--require-cuda", action="store_true")
    args = parser.parse_args()
    report = check(args.profile, args.require_cuda)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
