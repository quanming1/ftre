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


def test_f32_runtime_dependency_baseline_has_only_public_services() -> None:
    """F32 后 Runtime 只接收公开 Service，旧直连依赖不得回归。"""
    provider = (RUNTIME / "provider.py").read_text(encoding="utf-8")
    assert "message_bus=ctx.message_bus" in provider
    assert "sessions=ctx.sessions" in provider
    assert "tools=ctx.tools" in provider
    assert "profiles=ctx.agent_profiles" in provider
    assert "ctx.channels" not in provider
    assert "ctx.get(\"mcp\"" not in provider
    assert "ctx.agent_profiles.manager" not in provider

    engine = (RUNTIME / "engine.py").read_text(encoding="utf-8")
    for marker in ("channel_manager=None,", "tool_registry: ToolRegistry | None = None,",
                   "mcp_service=None,", "agent_manager=None,", "self.session_projection ="):
        assert marker not in engine, marker
    assert "self.message_bus = message_bus" in engine
    assert "self.sessions = sessions" in engine

    turn = (RUNTIME / "turn_executor.py").read_text(encoding="utf-8")
    for marker in (
        "from ftre.services.tools.builtin._workspace import",
        "loop.agent_manager._default_agent_state()",
        "loop.agent_manager.create_agent(",
        "WorkspaceAccessor(",
        "self._loop.session_projection.finish_open(",
        "load_config()",
    ):
        assert marker not in turn, marker
    assert "self._sessions = sessions" in turn
    assert "self._tools = tools" in turn
    assert "self._profiles = profiles" in turn
    assert "from ftre.services.agent.registry import AgentRegistry" not in (
        (RUNTIME / "engine.py").read_text(encoding="utf-8")
    )


def test_f31_runtime_does_not_use_context_as_service_locator() -> None:
    """Runtime 只能消费已组装字段；Provider 的 optional ctx.get 不属于运行时。"""
    for path in (RUNTIME / "engine.py", RUNTIME / "turn_executor.py"):
        assert _ctx_get_calls(path) == [], path


def test_f32_runtime_has_no_private_owner_imports() -> None:
    """Runtime 不得导入其他 Owner 的 Manager、Repository 或 builtin 私有实现。"""
    all_runtime_imports = tuple(
        module
        for path in RUNTIME.rglob("*.py")
        for module in _imports(path)
    )
    private_modules = {
        module
        for module in all_runtime_imports
        if module.startswith("ftre.services.")
        and (".builtin." in module or module.endswith(".manager") or ".persistence" in module)
    }
    assert private_modules == set()


def test_f32_runtime_uses_public_bus_and_session_exits() -> None:
    """Runtime 只能调用 Service 窄出口，不能把底层 EventBus 当作依赖。"""
    runtime_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (RUNTIME / "engine.py", RUNTIME / "turn_executor.py")
    )
    assert "EventBus" not in runtime_sources
    assert "message_bus.bus" not in runtime_sources
    assert "publish_outbound(" in runtime_sources
    assert "finish_open_replies(" in runtime_sources


def test_f32_turn_input_and_core_creation_have_single_owner() -> None:
    """InboundMessage 和 Core 工厂边界必须保持单向，避免 Runtime 再造协议。"""
    engine = (RUNTIME / "engine.py").read_text(encoding="utf-8")
    turn = (RUNTIME / "turn_executor.py").read_text(encoding="utf-8")
    factory = (RUNTIME / "factory.py").read_text(encoding="utf-8")
    assert "InboundMessage(" in engine
    assert "inbound: InboundMessage" in turn
    assert "self._core_factory(" in turn
    assert "return ReActAgent(" in factory
    assert "ReActAgent(" not in turn.replace("ReActAgent | None", "")
    assert "or AgentRegistry()" not in engine


def test_f32_llm_hook_callback_does_not_use_context_as_locator() -> None:
    """异步 adapters-updated 回调必须使用 apply 阶段已解析的 HookRuntime。"""
    source = (SRC / "services" / "llm" / "plugin.py").read_text(encoding="utf-8")
    assert "hook_runtime = ctx.hook_runtime" in source
    assert "return await hook_runtime.dispatch" in source
    assert "ctx.hook_runtime.dispatch" not in source


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
