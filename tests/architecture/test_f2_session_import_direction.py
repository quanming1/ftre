from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2] / "src" / "ftre" / "services" / "session"


def test_session_owner_does_not_import_legacy_session_modules() -> None:
    violations: list[str] = []
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("ftre.session"):
                violations.append(f"{path}:{node.lineno}: from {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "ftre.session" or alias.name.startswith("ftre.session."):
                        violations.append(f"{path}:{node.lineno}: import {alias.name}")
    assert violations == []
