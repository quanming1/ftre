"""Provider Plugin for the route-contribution registry.

This creates a registry, not a listening server. The App Host decides when the
registry is converted into a FastAPI application and when it starts uvicorn.
"""

from __future__ import annotations

from cordis import Context

from .service import HttpService

provide = ("http",)
inject = ()


def apply(ctx: Context, config=None):
    """Publish the HTTP registry unless an embedded host supplied one."""
    if ctx.get("http", strict=False) is not None:
        return
    service = HttpService()
    ctx.provide("http", service)
