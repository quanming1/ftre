"""
ftre 工具集
"""
from ftre_agent_core.tool import Tool
from .think import create_think_tool


def get_default_tools() -> list[Tool]:
    """获取默认工具集"""
    return [
        create_think_tool(),
    ]
