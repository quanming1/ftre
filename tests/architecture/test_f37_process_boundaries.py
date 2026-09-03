from __future__ import annotations

import ast
from pathlib import Path

from ftre.app.gateway.composition import default_manifests

ROOT = Path(__file__).parents[2]


def test_process_provider_precedes_core_tools() -> None:
    manifests = default_manifests()
    ids = [manifest.id for manifest in manifests]
    assert "process" in ids
    assert ids.index("process") < ids.index("core-tools")
    process = next(manifest for manifest in manifests if manifest.id == "process")
    assert process.required is True
    assert process.default_enabled is True


def test_core_tools_declares_process_injection() -> None:
    source = (ROOT / "src/ftre/plugins/builtin/core_tools/plugin.py").read_text(encoding="utf-8")
    assert 'inject = ("tools", "process")' in source
    bash = (ROOT / "src/ftre/plugins/builtin/core_tools/bash.py").read_text(encoding="utf-8")
    assert "ProcessService" in bash
    assert "subprocess.Popen" not in bash
    assert "subprocess.run" not in bash


def test_agent_runtime_declares_and_forwards_process_injection() -> None:
    plugin = (ROOT / "packages/ftre-agent-runtime/src/ftre_agent_runtime/plugin.py").read_text(
        encoding="utf-8"
    )
    engine = (ROOT / "packages/ftre-agent-runtime/src/ftre_agent_runtime/engine.py").read_text(
        encoding="utf-8"
    )
    turn_executor = (ROOT / "packages/ftre-agent-runtime/src/ftre_agent_runtime/turn_executor.py").read_text(
        encoding="utf-8"
    )
    assert '"process",' in plugin
    assert "process_service=ctx.process" in plugin
    assert "process_service=None" in engine
    assert '"process": self._process_service' in turn_executor


def test_host_production_has_no_direct_process_creation() -> None:
    forbidden = {"run", "Popen", "call", "check_call", "check_output", "check_run"}
    for path in (ROOT / "src/ftre").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "subprocess" and node.func.attr in forbidden:
                raise AssertionError(f"裸 subprocess 调用：{path}:{node.lineno}")
            if (
                isinstance(owner, ast.Attribute)
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "asyncio"
                and owner.attr.startswith("create_subprocess")
            ):
                raise AssertionError(f"裸 asyncio subprocess 调用：{path}:{node.lineno}")
