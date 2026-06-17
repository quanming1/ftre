"""
ftre 内置工具集

工具是无状态的工厂产物。当前工作区是 sessions 表的一等字段，agent 每次 run
通过 runtime_context['workspace'] = WorkspaceAccessor(...) 注入一个对 DB 的
同步外观，工具用 Injected("workspace") 拿到它后调 ws.get() / ws.set(...)
读写持久化的 cwd。
"""
from ftre_agent_core.tool import Tool

from .bash import create_bash_tool
from .cron import create_cron_tool
from .edit import create_edit_tool
from .read import create_read_tool
from .see_img import create_see_img_tool
from .send_message import create_send_message_tool
from .set_workspace import create_set_workspace_tool
from .task import create_task_tool
from .write import create_write_tool


class ToolRegistry:
    """运行时工具注册表，供插件注册额外 Tool。"""

    def __init__(self) -> None:
        self._tools: list[Tool] = []

    def register(self, tool: Tool) -> None:
        """注册一个工具；工具名不能重复。"""
        if any(getattr(t, "name", None) == getattr(tool, "name", None) for t in self._tools):
            raise ValueError(f"tool already registered: {getattr(tool, 'name', '')}")
        self._tools.append(tool)

    def snapshot(self) -> list[Tool]:
        """返回当前已注册工具的快照。"""
        return list(self._tools)

    def truncate(self, size: int) -> None:
        """回滚到指定大小，用于插件 setup 失败时清理。"""
        del self._tools[size:]

    def __len__(self) -> int:
        return len(self._tools)


def build_default_tools(channel_manager=None, tool_registry: ToolRegistry | None = None) -> list[Tool]:
    """构建默认工具集：bash + read + write + edit + set_workspace + cron
    + task + send_message

    Args:
        channel_manager: ChannelManager 实例（用于 send_message 工具）
    """
    tools = [
        create_bash_tool(),
        create_read_tool(),
        create_write_tool(),
        create_edit_tool(),
        create_set_workspace_tool(),
        create_cron_tool(),
        create_see_img_tool(),
    ]

    if channel_manager:
        tools.append(create_task_tool(channel_manager))
        tools.append(create_send_message_tool(channel_manager))

    if tool_registry is not None:
        tools.extend(tool_registry.snapshot())

    return tools
