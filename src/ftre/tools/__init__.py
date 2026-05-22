"""
ftre 内置工具集

每次 build_default_tools() 创建独立的工具实例（带共享的 cwd 状态），
保证 session 之间隔离。
"""
import os

from ftre_agent_core.tool import Tool
from .bash import create_bash_tool, _BashState
from .cron import create_cron_tool
from .edit import create_edit_tool
from .read import create_read_tool
from .send_message import create_send_message_tool
from .think import create_think_tool
from .write import create_write_tool


def build_default_tools(cwd: str | None = None, channel_manager=None) -> list[Tool]:
    """构建默认工具集：think + bash + read + write + edit + cron + send_message

    Args:
        cwd: 工作目录（默认使用进程 CWD）
        channel_manager: ChannelManager 实例（用于 send_message 工具）
    """
    state = _BashState(cwd or os.getcwd())
    tools = [
        create_think_tool(),
        create_bash_tool(state=state),
        create_read_tool(state=state),
        create_write_tool(state=state),
        create_edit_tool(state=state),
        create_cron_tool(),
    ]

    if channel_manager:
        tools.append(create_send_message_tool(channel_manager))

    return tools
