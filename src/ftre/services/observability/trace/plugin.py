"""Provider Plugin for durable Agent execution traces."""

from __future__ import annotations

from cordis import Context

from .service import TraceService

provide = ("traces",)
inject = ()


def apply(ctx: Context, config=None):
    """Publish TraceService and close its exporter when the Fiber unloads."""
    if ctx.get("traces", strict=False) is not None:
        return
    service = TraceService()
    ctx.provide("traces", service)
