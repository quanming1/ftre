"""Session Service 的 Provider Plugin。

Session 是 Agent 数据面的持久化根，因此 Provider 先创建并初始化 repository，再
发布 ``sessions``；Hook runtime 通过注入 key 绑定 lifecycle 桥，而不是由
SessionService 反向查找全局 AgentLoop。
"""

from __future__ import annotations

from cordis import Context

from .events import SessionEventService
from .hooks import (
    SESSION_CREATED_SPEC,
    SESSION_DISPOSED_SPEC,
    SessionLifecyclePayload,
)
from .service import SessionService

inject = ("hook_runtime", "http")
provide = ("sessions", "session_events")


async def apply(ctx: Context, config=None):
    """先初始化 Session 存储，再发布 ``sessions`` Service。"""
    if ctx.get("session_events", strict=False) is None:
        ctx.provide("session_events", SessionEventService())
    service = ctx.get("sessions", strict=False)
    if service is None:
        options = config if isinstance(config, dict) else {}
        service = SessionService(sessions_dir=options.get("sessions_dir"))
        await service.init()
        ctx.provide("sessions", service)

    async def dispatch(kind: str, session_id: str, channel_id: str) -> None:
        spec = SESSION_CREATED_SPEC if kind == "created" else SESSION_DISPOSED_SPEC
        await ctx.hook_runtime.dispatch(
            spec,
            SessionLifecyclePayload(session_id, channel_id),
        )

    # Composition tests and embedders may provide a narrow Session contract
    # instead of the default implementation; only the owned implementation
    # participates in these lifecycle hooks.
    if isinstance(service, SessionService):
        unbind = service.bind_lifecycle_dispatcher(dispatch)
        ctx.effect(lambda: unbind, label="hook:session:lifecycle")
        ctx.effect(lambda: service.close, label="sessions:close")

    from .router import build_router

    disposer = ctx.http.register_router(
        build_router(
            service,
            lambda: ctx.get("agents", strict=False),
            lambda: ctx.get("inbox", strict=False),
        ),
        owner="sessions",
    )
    ctx.effect(lambda: disposer, label="http:sessions")
