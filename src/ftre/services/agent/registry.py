"""Agent 运行时身份 Registry。

Registry 只保存稳定的 Agent identity、状态和 scope 信息，不创建 AgentLoop，也
不持有 Inbox 或 AgentLoop。删除后再次注册同一个字符串 id 会得到新的 identity，避免
旧 scope 监听器命中新生命周期的 Agent。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ftre.platform.hooks import HookScopeCarrier


@dataclass(slots=True)
class AgentRecord:
    """一个 Agent 生命周期内稳定的 identity 和可观测状态。"""
    agent_id: str
    identity: object
    state: str = "ready"

    def summary(self) -> dict[str, Any]:
        """返回不暴露 identity 对象的诊断摘要。"""
        return {"id": self.agent_id, "state": self.state}


class AgentRegistry:
    """管理 Agent runtime identity，不管理 AgentLoop 算法。"""

    def __init__(self) -> None:
        self._records: dict[str, AgentRecord] = {}

    def register(self, agent_id: str, *, state: str = "ready") -> AgentRecord:
        """注册或更新 Agent；只有首次注册才创建新的 scope identity。"""
        if not agent_id.strip():
            raise ValueError("agent_id must be non-empty")
        record = self._records.get(agent_id)
        if record is None:
            record = AgentRecord(agent_id=agent_id, identity=object(), state=state)
            self._records[agent_id] = record
        else:
            record.state = state
        return record

    def ensure(self, agent_id: str, *, state: str = "ready") -> AgentRecord:
        """Return an existing identity or create it exactly once."""
        return self._records.get(agent_id) or self.register(agent_id, state=state)

    def set_state(self, agent_id: str, state: str) -> None:
        record = self._records.get(agent_id)
        if record is None:
            raise KeyError(agent_id)
        record.state = state

    def dispose(self, agent_id: str) -> bool:
        """结束 Agent 生命周期，使旧 scope identity 不再命中新记录。"""
        return self._records.pop(agent_id, None) is not None

    def list(self) -> list[dict[str, Any]]:
        return [record.summary() for record in self._records.values()]

    def get(self, agent_id: str) -> dict[str, Any] | None:
        record = self._records.get(agent_id)
        return record.summary() if record is not None else None

    def tool_scope(self, agent_id: str) -> str:
        if agent_id not in self._records:
            raise KeyError(agent_id)
        return f"agent:{agent_id}"

    def scope_identity(self, agent_id: str) -> object:
        record = self._records.get(agent_id)
        if record is None:
            raise KeyError(agent_id)
        return record.identity

    def scope_carrier(
        self, agent_id: str, *, parent_id: str | None = None
    ) -> HookScopeCarrier:
        parent = None
        if parent_id is not None:
            parent = HookScopeCarrier("agent", self.scope_identity(parent_id))
        return HookScopeCarrier("agent", self.scope_identity(agent_id), parent=parent)


__all__ = ["AgentRecord", "AgentRegistry"]
