from __future__ import annotations

import ast
from pathlib import Path

from ftre.agent.agent_manager import AgentManager as LegacyAgentManager
from ftre.agent.loop import AgentLoop as LegacyAgentLoop
from ftre.services.agent.profile.manager import AgentManager
from ftre.services.agent.runtime.loop.engine import AgentLoop

ROOT = Path(__file__).parents[2] / "src" / "ftre" / "services" / "agent"


def test_legacy_agent_modules_resolve_to_new_runtime_owners() -> None:
    assert LegacyAgentManager is AgentManager
    assert LegacyAgentLoop is AgentLoop
    assert "services\\agent" in str(Path(LegacyAgentLoop.__module__.replace(".", "\\")))


def test_new_agent_owner_does_not_import_legacy_agent_modules() -> None:
    violations: list[str] = []
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("ftre.agent"):
                violations.append(f"{path}:{node.lineno}: from {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "ftre.agent" or alias.name.startswith("ftre.agent."):
                        violations.append(f"{path}:{node.lineno}: import {alias.name}")
    assert violations == []
