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

    # Composition tests and embedders may provide a narrow Session contract
    # instead of the default implementation; only the owned implementation
    # participates in these lifecycle hooks.
    if isinstance(service, SessionService):
        unbind = service.bind_lifecycle_dispatcher(dispatch)
        ctx.effect(lambda: unbind, label="hook:session:lifecycle")
        ctx.effect(service.close, label="sessions:close")
