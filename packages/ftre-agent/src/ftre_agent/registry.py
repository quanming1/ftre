"""Agent 身份 Registry 与 Hook 作用域载体。

Registry 只保存稳定的 Agent identity、状态和 scope 信息，不创建 AgentLoop，
也不持有 Inbox 或任何队列状态。删除后再次注册同一个字符串 id 会得到新的
identity，避免旧 scope 监听器命中新生命周期的 Agent。

``HookScopeCarrier`` 原先定义在 ftre Host 的 kernel 中；由于契约包的
``AgentRegistry.scope_carrier()`` 必须构造它，而契约包不允许依赖 Host，F33
把该值类型唯一迁移到本模块。kernel 保留 ``context_for_scope`` 机制函数，
按鸭子类型消费 carrier 的 ``key``/``identity``/``identities`` 属性。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class HookScopeCarrier:
    """一个运行时 Scope 身份及其可继承的祖先链。

    ``identity`` 必须是 Agent 生命周期对象，而不是可复用的字符串 id；同 id
    的新 Agent 应创建新的 identity。父 scope 的监听器会命中后代 scope，兄弟
    scope 不会互相命中。这个对象是不可变的，创建后不能把某个监听器悄悄移动
    到另一个 Agent。
    """

    key: str
    identity: object
    parent: HookScopeCarrier | None = None

    def __post_init__(self) -> None:
        # 空 key 会让隔离 Context 无法稳定命名，也会让诊断难以定位。
        if not self.key.strip():
            raise ValueError("scope key must be non-empty")

        # parent 链使用对象身份而不是 ``==``：两个 Agent 可能实现了相同的
        # __eq__，但只要不是同一个生命周期对象，就不应被当成同一作用域。
        seen: list[object] = [self.identity]
        current = self.parent
        while current is not None:
            if any(current.identity is identity for identity in seen):
                raise ValueError("scope carrier parent chain contains a cycle")
            seen.append(current.identity)
            current = current.parent

    @property
    def identities(self) -> tuple[object, ...]:
        """返回"当前作用域 → 父作用域 → …"的身份快照。

        kernel 的 ``context_for_scope`` 用这组对象身份配置事件过滤器。返回
        tuple 而不是暴露内部链，确保一次 dispatch 期间作用域集合不会被外部修改。
        """
        values: list[object] = []
        current: HookScopeCarrier | None = self
        while current is not None:
            values.append(current.identity)
            current = current.parent
        return tuple(values)


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


__all__ = ["AgentRecord", "AgentRegistry", "HookScopeCarrier"]
