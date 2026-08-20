from __future__ import annotations

from cordis import PluginContext

from .service import ToolService

inject = ()
provide = ("tools",)


def apply(ctx: PluginContext, config=None):
    if ctx.optional("tools") is not None:
        return None
    ctx.provide("tools", ToolService())
