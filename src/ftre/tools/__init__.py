"""
ftre 内置工具集

工具是无状态的工厂产物。当前工作区是 sessions 表的一等字段，agent 每次 run
通过 runtime_context['workspace'] = WorkspaceAccessor(...) 注入一个对 DB 的
同步外观，工具用 Injected("workspace") 拿到它后调 ws.get() / ws.set(...)
读写持久化的 cwd。
"""
from ftre_agent_core.tool import Tool, ToolRegistry

from .bash import create_bash_tool
from .cron import create_cron_tool
from .edit import create_edit_tool
from .read import create_read_tool
from .send_message import create_send_message_tool
from .set_workspace import create_set_workspace_tool
from .task import create_task_tool
from .write import create_write_tool


def filter_tools(registry: ToolRegistry, tools_config: dict | None) -> ToolRegistry:
    """按 agent 的 tools.allow / tools.deny 在 registry 上原地过滤。

    Args:
        registry: 已注册所有工具的 ToolRegistry
        tools_config: agent.config.json 的 tools 字段，格式为
                      {"allow": [...], "deny": [...]} 或 None

    Returns:
        过滤后的同一个 registry（原地修改）。
        tools_config 为 None 时不做任何操作。
    """
    if not tools_config:
        return registry

    allow = set(tools_config.get("allow", []))
    deny = set(tools_config.get("deny", []))

    for name in list(registry.names):
        if name in deny:
            registry.unregister(name)
        elif allow and name not in allow:
            registry.unregister(name)

    return registry


def build_default_tools(
    channel_manager=None,
    tool_registry: ToolRegistry | None = None,
    llm_config=None,
) -> ToolRegistry:
    """构建默认工具集：bash + read + write + edit + set_workspace + cron
    + task + send_message + 插件注册的全局工具

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
    registry.register(create_cron_tool())

    if channel_manager:
        registry.register(create_task_tool(channel_manager))
        registry.register(create_send_message_tool(channel_manager))

    if tool_registry is not None:
        for tool in tool_registry.snapshot():
            registry.register(tool)

    return registry
