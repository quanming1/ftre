from __future__ import annotations

from cordis import PluginContext

from .service import MessageBusService

provide = ("message_bus",)
inject = ()


def apply(ctx: PluginContext, config=None):
    if ctx.optional("message_bus") is not None:
        return None
    service = MessageBusService()
    ctx.provide("message_bus", service)
