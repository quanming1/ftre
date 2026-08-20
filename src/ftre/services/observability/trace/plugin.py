from __future__ import annotations

from cordis import PluginContext

from .service import TraceService

provide = ("traces",)
inject = ()


def apply(ctx: PluginContext, config=None):
    if ctx.optional("traces") is not None:
        return None
    service = TraceService()
    ctx.provide("traces", service)
    close = getattr(service.store, "close", None)
    if close:
        ctx.effect(close, label="traces:close")
