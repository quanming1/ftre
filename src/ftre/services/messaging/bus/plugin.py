"""Provider Plugin for the business message bus."""

from __future__ import annotations

from cordis import Context

from .service import MessageBusService

provide = ("message_bus",)
inject = ()


def apply(ctx: Context, config=None):
    """Create the bus Service; channel providers consume its public facade."""
    if ctx.get("message_bus", strict=False) is not None:
        return
    service = MessageBusService()
    ctx.provide("message_bus", service)
