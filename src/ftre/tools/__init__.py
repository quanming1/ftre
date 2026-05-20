"""
ftre 内置工具集

每次 get_default_tools() 创建独立的工具实例（带共享的 cwd 状态），
保证 session 之间隔离。
"""
import os

from ftre_agent_core.tool import Tool
from .bash import create_bash_tool, _BashState
from .edit import create_edit_tool
from .read import create_read_tool
from .think import create_think_tool
from .write import create_write_tool


def get_default_tools(cwd: str | None = None) -> list[Tool]:
    """获取默认工具集：think + bash + read + write + edit

    Args:
        cwd: 工作目录（默认使用进程 CWD）。bash/read/write/edit 共享此 cwd，
             bash 中的 `cd` 会更新这个 cwd，影响后续所有工具的相对路径解析。
    """
    state = _BashState(cwd or os.getcwd())
    return [
        create_think_tool(),
        create_bash_tool(state=state),
        create_read_tool(state=state),
        create_write_tool(state=state),
        create_edit_tool(state=state),
    ]
