from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2] / "src" / "ftre"
LEGACY_ROOTS = ("session", "agent", "bus", "channel", "command", "tools")


def test_migrated_legacy_modules_are_shims_not_second_implementations() -> None:
    violations: list[str] = []
    for name in LEGACY_ROOTS:
        root = ROOT / name
        for path in root.rglob("*.py"):
            if path.name in {"__init__.py", "test_channel.py"}:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            implementations = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            if implementations:
                violations.append(f"{path}: {len(implementations)} implementation definitions")
            if "services." not in path.read_text(encoding="utf-8"):
                violations.append(f"{path}: no services Owner reference")
    assert violations == []
