"""Agent / Session scoped 工具 allow/deny 限制模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolRestriction:
    """由某个 owner 添加、可逆移除的 Agent 或 Session 工具限制。

    ``max_scope`` 让高优先级 contribution 可以覆盖低层禁用。例如项目 MCP
    覆盖 Agent 中已禁用的同名全局 MCP 时，Agent restriction 只能隐藏 global
    contribution，不能再误伤 project 的 session contribution。
    """
    agent_id: str
    owner: str
    allow: frozenset[str] = field(default_factory=frozenset)
    deny: frozenset[str] = field(default_factory=frozenset)
    session_id: str | None = None
    max_scope: str = "session"
