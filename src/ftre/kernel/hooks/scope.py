"""Hook 作用域的身份载体和 Cordis Context 工厂。

Hook 的作用域不是简单的字符串过滤。比如一个名为 ``default`` 的 Agent 被重启
后，新旧两个运行实例可能拥有相同的 Agent id，但它们必须拥有不同的监听器和
in-flight 状态。因此这里用“生命周期对象身份”作为真正的隔离键，字符串只作为
日志和诊断标签。

``HookScopeCarrier.parent`` 还表达了作用域继承：父 Agent 的全局策略可以命中
子 Agent，而兄弟 Agent 不会互相收到 Hook。最后由 ``context_for_scope`` 把这个
身份规则交给 Cordis 的 isolate Context，而不是在 ftre 自己维护第二套事件总线。
"""

from __future__ import annotations

from dataclasses import dataclass

from cordis import Context


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
        """返回“当前作用域 → 父作用域 → …”的身份快照。

        ``context_for_scope`` 用这组对象身份配置事件过滤器。返回 tuple 而不是
        暴露内部链，确保一次 dispatch 期间作用域集合不会被外部修改。
        """
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

    返回的 Context 是 dispatch/register 时使用的“视图”，不是新的 HookRuntime。
    所有监听器仍由根 Context 的 Cordis EventsService 管理，避免每个 Agent 创建
    一套孤立的事件系统。
    """
    scoped = base.isolate(carrier.key, carrier.identity)
    allowed = carrier.identities

    # Cordis 触发事件时会把事件来源 Context 传入过滤器。只有来源身份是当前
    # carrier 或其祖先时才放行；同名但重新创建的 Agent identity 不会匹配。
    scoped.event_filter = lambda hook_ctx: any(
        hook_ctx._isolate.get(carrier.key) is identity for identity in allowed
    )
    return scoped


__all__ = ["HookScopeCarrier", "context_for_scope"]
