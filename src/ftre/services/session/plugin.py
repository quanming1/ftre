"""Session Service 的 Provider Plugin。

Session 是 Agent 数据面的持久化根，因此 Provider 先创建并初始化 repository，再
发布 ``sessions``；SessionService 直接使用注入的 Hook Runtime 发布 lifecycle，
不反向查找全局 AgentLoop。
"""

from __future__ import annotations

from cordis import Context

from .events import SessionEventService
from .service import SessionService

inject = ("hook_runtime", "message_bus")
provide = ("sessions", "session_events")


async def apply(ctx: Context, config=None):
    """先初始化 Session 存储，再发布 ``sessions`` Service。"""
    service = ctx.get("sessions", strict=False)
    if service is None:
        options = config if isinstance(config, dict) else {}
        service = SessionService(
            sessions_dir=options.get("sessions_dir"),
            hook_runtime=ctx.hook_runtime,
        )
        await service.init()
        ctx.provide("sessions", service)

    if ctx.get("session_events", strict=False) is None:
        ctx.provide(
            "session_events",
            SessionEventService(service, ctx.message_bus),
        )

    # Composition tests and embedders may provide a narrow Session contract
    # instead of the default implementation; only the owned implementation
    # participates in these lifecycle hooks.
    if isinstance(service, SessionService):
        ctx.effect(lambda: service.close, label="sessions:close")
