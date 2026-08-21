"""
ftre 内置工具集

工具是无状态的工厂产物。当前工作区是 sessions 表的一等字段，agent 每次 run
通过 runtime_context['workspace'] = WorkspaceAccessor(...) 注入一个对 DB 的
同步外观，工具用 Injected("workspace") 拿到它后调 ws.get() / ws.set(...)
读写持久化的 cwd。
"""
from ftre_agent_core.tool import ToolRegistry

from .bash import create_bash_tool
from .edit import create_edit_tool
from .read import create_read_tool
from .send_message import create_send_message_tool
from .set_workspace import create_set_workspace_tool
from .task import create_task_tool
from .team import create_team_tools
from .write import create_write_tool


def coerce_tool_name_list(value, field: str) -> list[str]:
    """tools.allow / tools.deny 规范化。

    None → []；单个字符串宽容为单元素列表（allow="bash" 语义即"只放行 bash"）；
    其余必须是字符串列表，否则抛 ValueError——宁可显式失败，
    也不能让畸形配置静默清空成员的全部工具。
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(  # noqa: TRY004 legacy compatibility boundary reviewed in F1
            f"tools.{field} 必须是字符串列表，实际: {type(value).__name__}"
        )
    bad = [x for x in value if not isinstance(x, str) or not x.strip()]
    if bad:
        raise ValueError(f"tools.{field} 含非字符串或空元素: {bad!r}")
    return value


def filter_tools(registry: ToolRegistry, tools_config: dict | None) -> ToolRegistry:
    """按 agent 的 tools.allow / tools.deny 在 registry 上原地过滤。

    Args:
        registry: 已注册所有工具的 ToolRegistry
        tools_config: agent.config.json 的 tools 字段，格式为
                      {"allow": [...], "deny": [...]} 或 None

    Returns:
        过滤后的同一个 registry（原地修改）。
        tools_config 为 None 时不做任何操作。

    Raises:
        ValueError: allow/deny 形状非法（防止静默清空工具）。
    """
    if not tools_config:
        return registry

    allow = set(coerce_tool_name_list(tools_config.get("allow"), "allow"))
    deny = set(coerce_tool_name_list(tools_config.get("deny"), "deny"))

    for name in list(registry.names):
        if name in deny or allow and name not in allow:
            registry.unregister(name)

    return registry


def build_default_tools(
    channel_manager=None,
    tool_registry: ToolRegistry | None = None,
    llm_config=None,
) -> ToolRegistry:
    """构建默认工具集：bash + read + write + edit + set_workspace
    + task + send_message + 插件注册的全局工具。

    Cron is contributed by ``features.schedule`` through ``ToolService``;
    keeping it out of this provider prevents a second, unowned registration.

    Args:
        channel_manager: ChannelManager 实例（用于 send_message / task 工具）
        tool_registry: 全局插件 ToolRegistry，其工具会被合并进来
        llm_config: 当前 Agent 的 llm 配置

    Returns:
        一个新的 ToolRegistry，包含内置工具 + 全局插件工具。
    """
    registry = ToolRegistry()

    registry.register(create_bash_tool())
    registry.register(create_read_tool(vision=getattr(llm_config, "vision", False)))
    registry.register(create_write_tool())
    registry.register(create_edit_tool())
    registry.register(create_set_workspace_tool())

    if channel_manager:
        registry.register(create_task_tool(channel_manager))
        registry.register(create_send_message_tool(channel_manager))
        for tool in create_team_tools(channel_manager):
            registry.register(tool)

    if tool_registry is not None:
        for tool in tool_registry.snapshot():
            registry.register(tool)

    return registry
