"""Provider Plugin for global and scoped tool contributions."""

from __future__ import annotations

from cordis import PluginContext

from .service import ToolService

inject = ()
provide = ("tools",)


def apply(ctx: PluginContext, config=None):
    """Publish the ToolService used by built-in and external Features."""
    if ctx.optional("tools") is not None:
        return
    ctx.provide("tools", ToolService())
