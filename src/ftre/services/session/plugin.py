"""Provider Plugin for Session persistence and lifecycle."""

from __future__ import annotations

from cordis import PluginContext

from .service import SessionService

inject = ()
provide = ("sessions",)


async def apply(ctx: PluginContext, config=None):
    """Initialize the session store before making ``sessions`` visible."""
    if ctx.optional("sessions") is not None:
        return
    options = config if isinstance(config, dict) else {}
    service = SessionService(sessions_dir=options.get("sessions_dir"))
    await service.init()
    ctx.provide("sessions", service)
    ctx.effect(service.close, label="sessions:close")
