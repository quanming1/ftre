"""F31/F32 Agent Runtime 边界门禁（F33 终局版）。

F31/F32 把当时的真实债务锁成可审计基线；F33 完成 Package 抽取后，这些门禁
升级为终局断言：Runtime 位于 ``packages/ftre-agent-runtime``，唯一 Owner 由
entry point 装载，且 Runtime 源码不得 import 任何 ``ftre.services.*`` 实现。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from ftre_agent import (
    AGENT_AFTER_RUN_SPEC,
    AGENT_BEFORE_RUN_SPEC,
    AGENT_RUN_ERROR_SPEC,
    AGENT_STOP_DECISION_SPEC,
)

from ftre.app.gateway.composition import default_manifests
from ftre.services.llm.hooks import (
    ADAPTERS_UPDATED_SPEC,
    AGENT_REQUEST_SPEC,
    LLM_STREAM_SPEC,
)
from ftre.services.system_prompt.hooks import SYSTEM_PROMPT_ASSEMBLE_SPEC

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"
RUNTIME = ROOT / "packages" / "ftre-agent-runtime" / "src" / "ftre_agent_runtime"


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
    """提取 ``ctx.get(...)``，只检查 Runtime 执行层，不误伤 Provider 的可选依赖解析。"""
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


def _provider_files() -> list[Path]:
    """Host services 与 packages 的全部 Provider Plugin 文件。"""
    files = list((SRC / "services").rglob("plugin.py"))
    files.extend((ROOT / "packages").glob("*/src/*/plugin.py"))
    return files


def test_f31_service_provider_entries_have_one_owner() -> None:
    """F31 依赖图必须仍由 Composition + Provider Plugin 唯一声明。"""
    expected = {
        "agents": "ftre-agent-runtime",
        "sessions": "session",
        "session_events": "session",
        "message_bus": "bus",
        "tools": "tools",
        "system_prompt": "system_prompt",
        "agent_profiles": "agent/profile",
    }
    owners: dict[str, list[Path]] = {}
    for path in _provider_files():
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
    """F33 后 Runtime Plugin 只接收公开 Service，旧直连依赖不得回归。"""
    plugin = (RUNTIME / "plugin.py").read_text(encoding="utf-8")
    assert "message_bus=ctx.message_bus" in plugin
    assert "sessions=ctx.sessions" in plugin
    assert "tools=ctx.tools" in plugin
    assert "profiles=ctx.agent_profiles" in plugin
    assert "ctx.channels" not in plugin
    assert 'ctx.get("mcp"' not in plugin
    assert "ctx.agent_profiles.manager" not in plugin

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
    assert "from ftre_agent import AgentRegistry" not in engine


def test_f31_runtime_does_not_use_context_as_service_locator() -> None:
    """Runtime 执行层只能消费已组装字段；Provider 的 optional ctx.get 不属于运行时。"""
    for path in (RUNTIME / "engine.py", RUNTIME / "turn_executor.py"):
        assert _ctx_get_calls(path) == [], path


def test_f32_runtime_has_no_private_owner_imports() -> None:
    """F33 升级：Runtime 不得 import 任何 ``ftre.services.*`` 实现模块（AC21）。"""
    all_runtime_imports = tuple(
        module
        for path in RUNTIME.rglob("*.py")
        for module in _imports(path)
    )
    host_imports = {
        module
        for module in all_runtime_imports
        if module == "ftre" or module.startswith("ftre.")
    }
    assert host_imports == set(), sorted(host_imports)


def test_f32_runtime_uses_public_bus_and_session_exits() -> None:
    """Runtime 只能调用 Service 窄出口，不能把底层 EventBus 当作依赖。"""
    runtime_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (RUNTIME / "engine.py", RUNTIME / "turn_executor.py")
    )
    assert "EventBus" not in runtime_sources
    assert "message_bus.bus" not in runtime_sources
    assert "publish_session_status(" in runtime_sources
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
    # F33：Ftre Agent Hook 的唯一 Owner 是契约包 ftre_agent。
    assert AGENT_BEFORE_RUN_SPEC.payload_type.__module__ == "ftre_agent.hooks"
    assert AGENT_AFTER_RUN_SPEC.payload_type.__module__ == "ftre_agent.hooks"
    assert AGENT_RUN_ERROR_SPEC.payload_type.__module__ == "ftre_agent.hooks"


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
    # F33：Runtime 经 MessageBusService 窄出口发布状态，不 import BusMessage。
    assert "BusMessage" not in runtime_sources
    assert "publish_session_status(" in (RUNTIME / "engine.py").read_text(encoding="utf-8")
