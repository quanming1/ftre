"""F6.5 Agent Service/AgentLoop Provider 分层门禁（F33 Package 终局版）。"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
AGENT_SERVICE = ROOT / "packages" / "ftre-agent" / "src" / "ftre_agent" / "service.py"
AGENT_PLUGIN = ROOT / "packages" / "ftre-agent" / "src" / "ftre_agent" / "plugin.py"
RUNTIME_PLUGIN = ROOT / "packages" / "ftre-agent-runtime" / "src" / "ftre_agent_runtime" / "plugin.py"
BOOTSTRAP = ROOT / "src" / "ftre" / "app" / "gateway" / "bootstrap.py"
COMPOSITION = ROOT / "src" / "ftre" / "app" / "gateway" / "composition.py"


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
    assert not any("agent_runtime" in item for item in _imports(AGENT_SERVICE))
    assert "def _call(" not in source
    assert "getattr(self.loop" not in source
    assert "def register_factory(" in source
    assert "def attach_runtime(" not in source
    # F33：契约包不依赖 Host 与 Runtime 包。
    assert not any(
        item == "ftre" or item.startswith(("ftre.", "ftre_agent_runtime"))
        for item in _imports(AGENT_SERVICE)
    )


def test_agent_plugin_owns_only_the_service():
    source = AGENT_PLUGIN.read_text(encoding="utf-8")
    assert 'provide = ("agents",)' in source
    assert "AgentService()" in source
    assert "AgentLoop(" not in source


def test_runtime_plugin_only_registers_the_factory():
    source = RUNTIME_PLUGIN.read_text(encoding="utf-8")
    assert "AgentLoop(" in source
    assert 'provide = ()' in source
    assert "register_factory" in source
    assert 'provide = ("agents",)' not in source
    assert "AgentService(" not in source


def test_only_agent_loop_provider_constructs_agent_loop():
    plugin_source = RUNTIME_PLUGIN.read_text(encoding="utf-8")
    bootstrap_source = BOOTSTRAP.read_text(encoding="utf-8")
    composition_source = COMPOSITION.read_text(encoding="utf-8")
    assert "AgentLoop(" in plugin_source
    assert "AgentLoop(" not in bootstrap_source
    assert "AgentLoop(" not in composition_source
    assert "AgentLoopProvider" not in plugin_source
    assert "register_factory" in plugin_source
    assert "attach_runtime" not in bootstrap_source
    # Host 只通过 entry point 装载 Runtime，不手工组装。
    assert "ftre_agent_runtime.plugin:apply" in composition_source
