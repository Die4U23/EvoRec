"""Capture the current framework environment, without local editable paths."""

from importlib.metadata import distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    excluded = {"pip", "evorec", "setuptools", "wheel"}
    packages = {
        d.metadata["Name"].lower().replace("_", "-"): d.version
        for d in distributions()
        if d.metadata["Name"].lower().replace("_", "-") not in excluded
    }
    header = "# M1 service/database development snapshot; Python 3.12 / Windows.\n# Build tools are governed separately by pyproject.toml.\n"
    body = "".join(f"{name}=={version}\n" for name, version in sorted(packages.items()))
    (ROOT / "requirements-dev.lock.txt").write_text(header + body, encoding="utf-8")
    print(f"Locked {len(packages)} runtime/test dependencies")


if __name__ == "__main__":
    main()
