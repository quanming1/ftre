"""
ftre 内置工具集

工具是无状态的工厂产物；当前工作区由 agent 每次 run 时通过
runtime_context['workspace'] = {'cwd': str} 注入，
工具通过 Injected("workspace") 取得这个 mutable dict 引用。
"""
from ftre_agent_core.tool import Tool

from .bash import create_bash_tool
from .cron import create_cron_tool
from .edit import create_edit_tool
from .read import create_read_tool
from .send_message import create_send_message_tool
from .set_workspace import create_set_workspace_tool
from .task import create_task_tool
from .think import create_think_tool
from .write import create_write_tool


def build_default_tools(channel_manager=None) -> list[Tool]:
    """构建默认工具集：think + bash + read + write + edit + set_workspace + cron
    + task + send_message

    Args:
        channel_manager: ChannelManager 实例（用于 send_message 工具）
    """
    tools = [
        create_think_tool(),
        create_bash_tool(),
        create_read_tool(),
        create_write_tool(),
        create_edit_tool(),
        create_set_workspace_tool(),
        create_cron_tool(),
    ]

    if channel_manager:
        tools.append(create_task_tool(channel_manager))
        tools.append(create_send_message_tool(channel_manager))

    return tools
