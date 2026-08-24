from __future__ import annotations

import ast
from pathlib import Path

ROOTS = (
    Path(__file__).parents[2] / "src" / "ftre" / "app" / "gateway",
    Path(__file__).parents[2] / "src" / "ftre" / "services",
    Path(__file__).parents[2] / "src" / "ftre" / "plugins",
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


def test_http_routes_are_contributed_by_their_owner_plugins() -> None:
    source = (Path(__file__).parents[2] / "src" / "ftre" / "app" / "gateway" / "composition.py").read_text(encoding="utf-8")
    assert "register_compat_snapshot" not in source
    assert "register_router" not in source
    session_plugin = (Path(__file__).parents[2] / "src" / "ftre" / "services" / "session" / "plugin.py").read_text(encoding="utf-8")
    session_routes_plugin = (Path(__file__).parents[2] / "src" / "ftre" / "plugins" / "builtin" / "session_routes" / "plugin.py").read_text(encoding="utf-8")
    profile_plugin = (Path(__file__).parents[2] / "src" / "ftre" / "services" / "agent" / "profile" / "plugin.py").read_text(encoding="utf-8")
    assert 'owner="sessions"' in session_routes_plugin
    assert 'ctx.http.register_router' not in session_plugin
    assert 'owner="agent-profiles"' in profile_plugin
