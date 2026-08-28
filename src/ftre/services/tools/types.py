"""ToolService 注册贡献的不可变记录模型。"""

from __future__ import annotations

from dataclasses import dataclass

from ftre_agent.tool import ToolDefinition


@dataclass(frozen=True)
class ToolContribution:
    """记录声明名称、来源、Owner、scope 和执行契约。"""
    name: str
    owner: str
    source: str
    scope: str
    definition: ToolDefinition
