"""Agent scoped 工具 allow/deny 限制模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolRestriction:
    """由某个 owner 添加、可逆移除的单 Agent 工具限制。"""
    agent_id: str
    owner: str
    allow: frozenset[str] = field(default_factory=frozenset)
    deny: frozenset[str] = field(default_factory=frozenset)
