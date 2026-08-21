"""Hook scope carrier based on Cordis isolate Context identity."""

from __future__ import annotations

from dataclasses import dataclass

from cordis import Context


@dataclass(frozen=True, slots=True)
class HookScopeCarrier:
    """一个运行时 scope 身份及其可继承的祖先链。

    ``identity`` 必须是 Agent 生命周期对象，而不是可复用的字符串 id；同 id
    的新 Agent 应创建新的 identity。父 scope 的监听器会命中后代 scope，兄弟
    scope 不会互相命中。
    """

    key: str
    identity: object
    parent: HookScopeCarrier | None = None

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("scope key must be non-empty")
        seen: list[object] = [self.identity]
        current = self.parent
        while current is not None:
            if any(current.identity is identity for identity in seen):
                raise ValueError("scope carrier parent chain contains a cycle")
            seen.append(current.identity)
            current = current.parent

    @property
    def identities(self) -> tuple[object, ...]:
        """返回自身到全部祖先的 identity 快照。"""
        values: list[object] = []
        current: HookScopeCarrier | None = self
        while current is not None:
            values.append(current.identity)
            current = current.parent
        return tuple(values)


def context_for_scope(base: Context, carrier: HookScopeCarrier) -> Context:
    """创建带父子过滤语义的 Cordis isolate Context。

    Cordis 仍然负责监听器注册、Fiber owner 和事件快照；这里仅为官方
    ``Context._event_filter`` 提供 scope identity 的可继承判定。
    """
    scoped = base.isolate(carrier.key, carrier.identity)
    allowed = carrier.identities
    scoped.event_filter = lambda hook_ctx: any(
        hook_ctx._isolate.get(carrier.key) is identity for identity in allowed
    )
    return scoped


__all__ = ["HookScopeCarrier", "context_for_scope"]
