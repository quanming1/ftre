from __future__ import annotations

import ast
from pathlib import Path

ROOTS = (
    Path(__file__).parents[2] / "src" / "ftre" / "app" / "gateway",
    Path(__file__).parents[2] / "src" / "ftre" / "services",
    Path(__file__).parents[2] / "src" / "ftre" / "features",
)


def test_new_http_hosts_do_not_import_aggregate_api_or_setters() -> None:
    violations: list[str] = []
    for root in ROOTS:
        for path in root.rglob("*.py"):
            if path.name == "legacy.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in {"ftre.api.routes", "ftre.services.http.legacy"}:
                    violations.append(f"{path}:{node.lineno}: from {node.module}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in {"ftre.api.routes", "ftre.services.http.legacy"}:
                            violations.append(f"{path}:{node.lineno}: import {alias.name}")
    assert violations == []


def test_composition_route_snapshot_has_owner_routes() -> None:
    source = (Path(__file__).parents[2] / "src" / "ftre" / "app" / "gateway" / "composition.py").read_text(encoding="utf-8")
    assert "register_compat_snapshot" not in source
    assert "build_router(sessions, agents)" in source
    assert "build_router(profiles)" in source
