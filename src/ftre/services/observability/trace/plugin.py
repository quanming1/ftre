"""Provider Plugin for durable Agent execution traces."""

from __future__ import annotations

from cordis import PluginContext

from .service import TraceService

provide = ("traces",)
inject = ()


def apply(ctx: PluginContext, config=None):
    """Publish TraceService and close its exporter when the Fiber unloads."""
    if ctx.optional("traces") is not None:
        return
    service = TraceService()
    ctx.provide("traces", service)
    close = getattr(service.store, "close", None)
    if close:
        ctx.effect(close, label="traces:close")
