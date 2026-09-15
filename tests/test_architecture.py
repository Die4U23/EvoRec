"""Enforce static import boundaries; runtime adapter behavior needs integration tests."""

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "src" / "evorec"
ALLOWED = {
    "domain": {"domain"},
    "application": {"domain", "application"},
    "infrastructure": {"domain", "application", "infrastructure"},
    "api": {"domain", "application", "api", "contracts"},
}


def test_layers_respect_dependency_direction():
    violations = []
    for layer, allowed in ALLOWED.items():
        for path in (ROOT / layer).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [name.name for name in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        violations.append(f"{path.name}:{node.lineno}: use absolute imports for boundary checking")
                    modules = [node.module or ""]
                    if node.module == "evorec":
                        modules = [f"evorec.{name.name}" for name in node.names]
                else:
                    continue
                for module in modules:
                    top = module.split(".")[0]
                    if layer in {"domain", "application"} and top not in sys.stdlib_module_names | {"evorec"}:
                        violations.append(f"{path.name}:{node.lineno}: external import {module}")
                    if module.startswith("evorec."):
                        target = module.split(".")[1]
                        composition_entry = path == ROOT / "api" / "app.py" and target == "bootstrap"
                        package_metadata = target == "__version__"
                        if target not in allowed and not composition_entry and not package_metadata:
                            violations.append(f"{path.name}:{node.lineno}: {layer} -> {target}")
    assert not violations, "\n".join(violations)
