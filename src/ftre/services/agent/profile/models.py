"""Agent profile 合并后的最小公开投影模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EffectiveProfile:
    """一个 Agent 在当前请求中解析出的最终配置视图。"""
    agent_id: str
    value: Any
