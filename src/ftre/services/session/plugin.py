"""Provider Plugin for Session persistence and lifecycle."""

from __future__ import annotations

from cordis import Context

from .hooks import (
    SESSION_CREATED_SPEC,
    SESSION_DISPOSED_SPEC,
    SessionLifecyclePayload,
)
from .service import SessionService

inject = ("hook_runtime",)
provide = ("sessions",)


async def apply(ctx: Context, config=None):
    """Initialize the session store before making ``sessions`` visible."""
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

    bind = getattr(service, "bind_lifecycle_dispatcher", None)
    if callable(bind):
        unbind = bind(dispatch)
        ctx.effect(lambda: unbind, label="hook:session:lifecycle")
    close = getattr(service, "close", None)
    if callable(close):
        ctx.effect(lambda: close, label="sessions:close")
