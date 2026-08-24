"""F6.5 Agent Service/AgentLoop Provider 分层门禁。"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
AGENT_SERVICE = ROOT / "src" / "ftre" / "services" / "agent" / "service.py"
AGENT_PLUGIN = ROOT / "src" / "ftre" / "services" / "agent" / "plugin.py"
PROVIDER = ROOT / "src" / "ftre" / "services" / "agent" / "runtime" / "provider.py"
BOOTSTRAP = ROOT / "src" / "ftre" / "app" / "gateway" / "bootstrap.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_public_agent_service_does_not_import_or_expose_agent_loop():
    source = AGENT_SERVICE.read_text(encoding="utf-8")
    assert not any("agent.runtime.loop" in item for item in _imports(AGENT_SERVICE))
    assert "def _call(" not in source
    assert "getattr(self.loop" not in source
    assert "def attach_driver(" in source


def test_agent_plugin_owns_the_service_and_private_runtime():
    source = AGENT_PLUGIN.read_text(encoding="utf-8")
    assert "build_runtime" in source
    assert "agent_runtime" not in source
    assert "options.get(\"loop\")" not in source
    assert 'provide = ("agents",)' in source


def test_only_agent_loop_provider_constructs_agent_loop():
    provider_source = PROVIDER.read_text(encoding="utf-8")
    bootstrap_source = BOOTSTRAP.read_text(encoding="utf-8")
    assert "AgentLoop(" in provider_source
    assert "AgentLoop(" not in bootstrap_source
    assert "AgentLoopProvider" not in provider_source
    assert "attach_driver" in AGENT_PLUGIN.read_text(encoding="utf-8")
    assert "AgentLoopProvider" not in bootstrap_source
    assert "attach_driver" not in bootstrap_source
