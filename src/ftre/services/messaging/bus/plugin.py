"""Provider Plugin for the business message bus."""

from __future__ import annotations

from cordis import PluginContext

from .service import MessageBusService

provide = ("message_bus",)
inject = ()


def apply(ctx: PluginContext, config=None):
    """Create the bus Service; channel providers consume its public facade."""
    if ctx.optional("message_bus") is not None:
        return
    service = MessageBusService()
    ctx.provide("message_bus", service)
