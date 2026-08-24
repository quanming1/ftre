"""ToolService 注册贡献的不可变记录模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolContribution:
    """记录工具名称、来源、Owner、scope 和实际工具对象。"""
    name: str
    owner: str
    source: str
    scope: str
    tool: Any
