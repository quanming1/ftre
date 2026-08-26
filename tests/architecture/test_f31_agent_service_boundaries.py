"""F31 Agent Runtime 边界门禁。

F31 不提前修改 AgentLoop，而是把当前真实债务锁成可审计的基线：后续 F32 删除
某一项时只需同步调整这里的断言。这样“暂时保留”不会悄悄变成新的依赖，且测试
本身不会引入生产 Protocol、Port 或 Service Locator。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from ftre.app.gateway.composition import default_manifests
from ftre.services.agent.hooks import (
    AGENT_AFTER_RUN_SPEC,
    AGENT_BEFORE_RUN_SPEC,
    AGENT_RUN_ERROR_SPEC,
    AGENT_STOP_DECISION_SPEC,
)
from ftre.services.llm.hooks import (
    ADAPTERS_UPDATED_SPEC,
    AGENT_REQUEST_SPEC,
    LLM_STREAM_SPEC,
)
from ftre.services.system_prompt.hooks import SYSTEM_PROMPT_ASSEMBLE_SPEC

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"
RUNTIME = SRC / "services" / "agent" / "runtime"


def _literal_names(path: Path, name: str) -> tuple[str, ...]:
    """静态读取 Plugin 声明，避免测试为了审计而启动完整 Composition。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List)):
            return ()
        return tuple(
            item.value
            for item in node.value.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
    return ()


def _imports(path: Path) -> tuple[str, ...]:
    """返回源码实际 import 的模块名，供跨 Owner 私有依赖门禁使用。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return tuple(modules)


def _ctx_get_calls(path: Path) -> list[ast.Call]:
    """提取 ``ctx.get(...)``，只检查 Runtime，不误伤 Provider 的可选依赖解析。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ctx"
    ]


def test_f31_service_provider_entries_have_one_owner() -> None:
    """F31 依赖图必须仍由 Composition + Provider Plugin 唯一声明。"""
    expected = {
        "agents": "agent",
        "sessions": "session",
        "session_events": "session",
        "message_bus": "bus",
        "tools": "tools",
        "system_prompt": "system_prompt",
        "agent_profiles": "agent/profile",
    }
    owners: dict[str, list[Path]] = {}
    for path in (SRC / "services").rglob("plugin.py"):
        for key in _literal_names(path, "provide"):
            owners.setdefault(key, []).append(path)

    manifests = {manifest.id: manifest for manifest in default_manifests()}
    for key, relative_owner in expected.items():
        matches = [path for path in owners.get(key, ()) if relative_owner in path.as_posix()]
        assert len(matches) == 1, f"{key} owner mismatch: {matches}"
        manifest_id = {
            "agents": "agents",
            "sessions": "sessions",
            "session_events": "sessions",
            "message_bus": "message-bus",
            "tools": "tools",
            "system_prompt": "system-prompt",
            "agent_profiles": "agent-profiles",
        }[key]
        assert manifest_id in manifests


def test_f31_manifest_entries_resolve_to_unique_plugin_callables() -> None:
    """Manifest 仍是 Composition 的唯一入口，入口模块必须可解析。"""
    manifests = default_manifests()
    assert len({manifest.id for manifest in manifests}) == len(manifests)
    for manifest in manifests:
        module_name, separator, attribute = manifest.entry_text.partition(":")
        assert separator and module_name and attribute, manifest.id
        target = getattr(importlib.import_module(module_name), attribute)
        assert callable(target), manifest.entry_text


def test_f31_runtime_dependency_baseline_is_explicit_and_cannot_grow() -> None:
    """锁定 F32 要删除的具体依赖；重复出现或新增同类入口会直接失败。"""
    provider = (RUNTIME / "provider.py").read_text(encoding="utf-8")
    provider_markers = (
        '"bus": ctx.message_bus.bus',
        '"channel_manager": ctx.channels.manager',
        '"tool_registry": tools.registry',
        '"mcp_service": ctx.get("mcp", strict=False)',
        '"agent_manager": ctx.agent_profiles.manager',
    )
    for marker in provider_markers:
        assert provider.count(marker) == 1, marker

    engine = (RUNTIME / "engine.py").read_text(encoding="utf-8")
    engine_markers = (
        "channel_manager=None,",
        "tool_registry: ToolRegistry | None = None,",
        "mcp_service=None,",
        "agent_manager=None,",
        "self.session_projection = session_manager.projection",
    )
    for marker in engine_markers:
        assert engine.count(marker) == 1, marker

    turn = (RUNTIME / "turn_executor.py").read_text(encoding="utf-8")
    turn_markers = (
        "from ftre.services.tools.builtin._workspace import (",
        "loop.agent_manager._default_agent_state()",
        "loop.agent_manager.create_agent(",
        "WorkspaceAccessor(",
        "self._loop.session_projection.finish_open(",
        "load_config()",
    )
    for marker in turn_markers:
        assert turn.count(marker) == 1, marker


def test_f31_runtime_does_not_use_context_as_service_locator() -> None:
    """Runtime 只能消费已组装字段；Provider 的 optional ctx.get 不属于运行时。"""
    for path in (RUNTIME / "engine.py", RUNTIME / "turn_executor.py"):
        assert _ctx_get_calls(path) == [], path


def test_f31_private_owner_imports_are_registered_baseline() -> None:
    """当前两个跨 Owner 私有导入有明确 F32 删除批次，不能再增加新的种类。"""
    turn_imports = _imports(RUNTIME / "turn_executor.py")
    assert turn_imports.count("ftre.services.tools.builtin._workspace") == 1
    assert turn_imports.count("ftre.services.agent.profile.manager") == 1
    private_modules = {
        module
        for module in turn_imports
        if module.startswith("ftre.services.")
        and (".builtin." in module or module.endswith(".manager"))
    }
    assert private_modules == {
        "ftre.services.tools.builtin._workspace",
        "ftre.services.agent.profile.manager",
    }


def test_f31_hook_specs_have_unique_names_and_real_owner_contracts() -> None:
    """Hook 名称、发布域和 payload/result 类型必须来自现有唯一 Spec。"""
    specs = (
        AGENT_BEFORE_RUN_SPEC,
        AGENT_AFTER_RUN_SPEC,
        AGENT_RUN_ERROR_SPEC,
        AGENT_STOP_DECISION_SPEC,
        AGENT_REQUEST_SPEC,
        LLM_STREAM_SPEC,
        ADAPTERS_UPDATED_SPEC,
        SYSTEM_PROMPT_ASSEMBLE_SPEC,
    )
    names = [spec.name for spec in specs]
    assert len(names) == len(set(names))
    assert {spec.name for spec in specs} == {
        "agent/before-run",
        "agent/after-run",
        "agent/run-error",
        "agent/stop-decision",
        "agent/request",
        "llm/stream",
        "llm/adapters-updated",
        "system-prompt/assemble",
    }
    assert AGENT_REQUEST_SPEC.payload_type.__module__ == "ftre_llm.contracts"
    assert LLM_STREAM_SPEC.name == "llm/stream"
    assert AGENT_STOP_DECISION_SPEC.payload_type.__module__.startswith("ftre_agent_core")
    assert ADAPTERS_UPDATED_SPEC.mode.value == "emit"


def test_f31_llm_request_publisher_and_channel_boundary_are_real() -> None:
    """确认 agent/request 由 LlmService 发布，Runtime 不复制发布逻辑。"""
    llm_source = (ROOT / "packages" / "ftre-llm" / "src" / "ftre_llm" / "service.py").read_text(
        encoding="utf-8"
    )
    runtime_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (RUNTIME / "engine.py", RUNTIME / "turn_executor.py")
    )
    assert '"agent/request"' in llm_source
    assert '"agent/request"' not in runtime_sources
    assert "InboundMessage" in (RUNTIME / "engine.py").read_text(encoding="utf-8")
    assert "BusMessage" in (RUNTIME / "engine.py").read_text(encoding="utf-8")
