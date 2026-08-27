"""F34 冻结的 ToolService 公开面与 core-tools 贡献契约。

覆盖：get/execute 的作用域语义、preparer 通用签名、内置工具作为普通贡献
（owner=core-tools）、profile allow/deny 不豁免内置工具的过滤语义。
"""

from __future__ import annotations

import pytest
from cordis import Context
from ftre_agent_core.tool import Tool, ToolRegistry

from ftre.plugins.builtin.core_tools import (
    create_bash_tool,
    create_edit_tool,
    create_read_tool,
    create_set_workspace_tool,
    create_write_tool,
)
from ftre.plugins.builtin.core_tools.plugin import apply as core_tools_apply
from ftre.services.tools import ToolService, filter_tools

CORE_TOOL_NAMES = {"bash", "read", "write", "edit", "set_workspace"}


def _core_tools_plugin(ctx, _config=None):
    return core_tools_apply(ctx, _config)


_core_tools_plugin.inject = ("tools",)
_core_tools_plugin.provide = ()


def _echo_tool(name: str) -> Tool:
    def echo(**kwargs) -> str:
        return f"echo:{name}"

    return Tool(name=name, description="echo", parameters=[], func=echo)


@pytest.mark.asyncio
async def test_core_tools_plugin_contributes_five_builtin_tools() -> None:
    """core-tools 是五个内置工具的唯一 Owner，贡献可逆。"""
    root = Context()
    tools = ToolService(ToolRegistry())
    root.provide("tools", tools)
    fiber = root.plugin(_core_tools_plugin)
    await fiber

    contributions = {item.name: item for item in tools.snapshot()}
    assert CORE_TOOL_NAMES <= set(contributions)
    for name in CORE_TOOL_NAMES:
        item = contributions[name]
        assert item.owner == "core-tools"
        assert item.source == "builtin"
        assert item.scope == "global"

    cleanup = fiber.dispose()
    if cleanup is not None:
        await cleanup
    assert CORE_TOOL_NAMES.isdisjoint({item.name for item in tools.snapshot()})
    cleanup = root.dispose()
    if cleanup is not None:
        await cleanup


def test_get_is_scope_aware_and_returns_none_for_unknown() -> None:
    """get() 按作用域解析；未知工具返回 None 而不是抛错。"""
    tools = ToolService()
    assert tools.get("bash") is None

    dispose = tools.register(_echo_tool("bash"), owner="core-tools")
    assert tools.get("bash") is not None
    assert tools.get("bash").owner == "core-tools"
    # agent 不可见（restrict deny）时 get 也必须反映出来。
    restrict = tools.restrict("worker", owner="policy", deny={"bash"})
    assert tools.get("bash", "worker") is None
    assert tools.get("bash", "other") is not None
    restrict()
    dispose()
    assert tools.get("bash") is None


def _labeled_tool(name: str, label: str) -> Tool:
    """返回值可区分的测试工具，用于验证 execute 到底执行了哪个版本。"""

    def run(**kwargs) -> str:
        return label

    return Tool(name=name, description="labeled", parameters=[], func=run)


def test_execute_agent_id_runs_scoped_shadow_not_global() -> None:
    """execute(agent_id) 执行作用域投影：scoped shadow 覆盖同名 global。"""
    tools = ToolService()
    dispose_global = tools.register(_labeled_tool("x", "global"), owner="og")
    dispose_scoped = tools.register(
        _labeled_tool("x", "scoped"), owner="os", scope="agent:a"
    )

    # get 与 execute 必须共享同一投影：agent a 解析到 scoped，其他人解析到 global。
    assert tools.get("x", "a").scope == "agent:a"
    assert tools.execute("x") == "global"
    assert tools.execute("x", agent_id="b") == "global"
    assert tools.execute("x", agent_id="a") == "scoped"

    dispose_global()
    dispose_scoped()


def test_execute_agent_id_supports_scoped_only_tools() -> None:
    """仅存在于 agent scope 的工具也能经 execute(agent_id) 执行。"""
    tools = ToolService()
    dispose = tools.register(
        _labeled_tool("only", "scoped"), owner="os", scope="agent:a"
    )

    assert tools.get("only") is None
    assert tools.execute("only", agent_id="a") == "scoped"
    # 全局路径不认识 scoped-only 工具（Core registry 缺名抛 ValueError）。
    with pytest.raises(ValueError, match="not found"):
        tools.execute("only")
    dispose()


def test_global_unload_leaves_no_residue_when_scoped_shadow_exists() -> None:
    """global+scoped 同名时卸载 global：_registry 不得残留可执行工具。"""
    tools = ToolService()
    dispose_global = tools.register(_labeled_tool("x", "global"), owner="og")
    dispose_scoped = tools.register(
        _labeled_tool("x", "scoped"), owner="os", scope="agent:a"
    )

    dispose_global()

    # global 视图已空（scoped 工具不进全局视图），且全局 execute 不再能
    # 执行已卸载的 global 工具——残留即生命周期泄漏。
    assert tools.snapshot() == ()
    with pytest.raises(ValueError, match="not found"):
        tools.execute("x")
    # agent 投影仍可解析并执行 scoped 版本。
    assert tools.get("x", "a") is not None
    assert tools.execute("x", agent_id="a") == "scoped"

    dispose_scoped()
    assert tools.snapshot() == ()
    assert tools.get("x", "a") is None
    with pytest.raises(KeyError, match="not visible"):
        tools.execute("x", agent_id="a")


def test_execute_validates_agent_visibility() -> None:
    """execute 传 agent_id 时先做可见性校验，不可见抛 KeyError。"""
    tools = ToolService()
    tools.register(_echo_tool("bash"), owner="core-tools")

    result = tools.execute("bash", arguments={})
    assert result == "echo:bash"
    assert tools.execute("bash", agent_id="worker") == "echo:bash"

    restrict = tools.restrict("worker", owner="policy", deny={"bash"})
    with pytest.raises(KeyError, match="not visible"):
        tools.execute("bash", agent_id="worker")
    # 其他 Agent 不受影响。
    assert tools.execute("bash", agent_id="other") == "echo:bash"
    restrict()


@pytest.mark.asyncio
async def test_view_preparer_receives_general_profile_contract() -> None:
    """preparer 契约是通用的四参签名，不再绑死 mcp_config 语义。"""
    tools = ToolService()
    seen: list[tuple] = []

    async def preparer(agent_id, session_id, profile_config, llm_config):
        seen.append((agent_id, session_id, profile_config, llm_config))

    dispose = tools.register_view_preparer(preparer, owner="test")
    llm_config = type("L", (), {"vision": True})()
    profile = {"mcp_config": {"a": 1}, "tools_config": None}
    await tools.prepare_view("default", "session-1", profile, llm_config=llm_config)

    assert len(seen) == 1
    agent_id, session_id, got_profile, got_llm = seen[0]
    assert (agent_id, session_id) == ("default", "session-1")
    # preparer 拿到的是完整 profile 片段与 llm_config，MCP 等消费者自行读字段。
    assert got_profile is profile
    assert got_llm is llm_config
    dispose()


@pytest.mark.asyncio
async def test_prepare_view_contains_builtin_tools_and_profile_filter_applies() -> None:
    """view 合并内置工具；profile allow/deny 不豁免内置工具（F34 冻结语义）。"""
    root = Context()
    tools = ToolService(ToolRegistry())
    root.provide("tools", tools)
    fiber = root.plugin(_core_tools_plugin)
    await fiber

    view = await tools.prepare_view("default", "session-1", {"tools_config": None})
    assert CORE_TOOL_NAMES <= set(view.names)

    filtered = await tools.prepare_view(
        "default",
        "session-1",
        {"tools_config": {"allow": ["bash", "read"]}},
    )
    # allow 是白名单：未列出的内置工具（write/edit/set_workspace）同样被过滤。
    assert set(filtered.names) == {"bash", "read"}

    denied = await tools.prepare_view(
        "default",
        "session-1",
        {"tools_config": {"deny": ["bash"]}},
    )
    assert "bash" not in denied.names
    assert CORE_TOOL_NAMES - {"bash"} <= set(denied.names)

    cleanup = fiber.dispose()
    if cleanup is not None:
        await cleanup
    cleanup = root.dispose()
    if cleanup is not None:
        await cleanup


def test_filter_tools_treats_builtin_and_plugin_tools_equally() -> None:
    """filter_tools 的 allow/deny 语义与工具来源无关（不豁免内置工具）。"""
    registry = ToolRegistry()
    for name in ["bash", "read", "cron"]:
        registry.register(_echo_tool(name))

    filter_tools(registry, {"allow": ["bash"]})
    assert registry.names == ["bash"]


def test_read_tool_description_is_model_neutral() -> None:
    """read 描述中性化：不再随 vision 能力改写，静态注册即可。"""
    import inspect

    signature = inspect.signature(create_read_tool)
    assert "vision" not in signature.parameters

    description = create_read_tool().description
    # 中性描述声明能力条件与运行时报错行为，但不替运行时断言模型能力。
    assert "识图" in description
    assert "不支持时会返回明确报错" in description
    assert "当前模型支持识图，可读取图片" not in description
    assert "当前模型不支持识图，无法读取图片" not in description


def test_all_five_factories_produce_distinct_instances() -> None:
    """工厂每次产出新实例，view 之间不共享可变状态。"""
    for factory in (
        create_bash_tool,
        create_read_tool,
        create_write_tool,
        create_edit_tool,
        create_set_workspace_tool,
    ):
        first = factory()
        second = factory()
        assert first is not second
        assert first.name == second.name
