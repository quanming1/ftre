"""Hook 作用域的 Cordis Context 工厂。

Hook 的作用域不是简单的字符串过滤。比如一个名为 ``default`` 的 Agent 被重启
后，新旧两个运行实例可能拥有相同的 Agent id，但它们必须拥有不同的监听器和
in-flight 状态。因此这里用“生命周期对象身份”作为真正的隔离键，字符串只作为
日志和诊断标签。

``HookScopeCarrier`` 值类型自 F33 起由契约包 ``ftre_agent`` 唯一提供（因为
``AgentRegistry.scope_carrier()`` 必须构造它，而契约包不依赖 Host）。本模块
只保留 ``context_for_scope`` 机制函数，按鸭子类型访问 carrier 的
``key``/``identity``/``identities``；这里的机制与 Agent 业务无关，kernel 不
import 任何业务包。
"""

from __future__ import annotations

from cordis import Context


def context_for_scope(base: Context, carrier) -> Context:
    """创建带父子过滤语义的 Cordis isolate Context。

    ``carrier`` 是任何提供 ``key``、``identity`` 与 ``identities`` 的作用域
    载体（当前唯一实现为 ``ftre_agent.HookScopeCarrier``）。

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


__all__ = ["context_for_scope"]
