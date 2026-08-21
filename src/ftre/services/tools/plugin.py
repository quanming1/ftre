"""Provider Plugin for global and scoped tool contributions."""

from __future__ import annotations

from cordis import Context

from .service import ToolService

inject = ()
provide = ("tools",)


def apply(ctx: Context, config=None):
    """Publish the ToolService used by built-in and external Features."""
    if ctx.get("tools", strict=False) is not None:
        return
    ctx.provide("tools", ToolService())
