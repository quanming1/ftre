from __future__ import annotations

from cordis import PluginContext

from .service import McpService

inject = ("config", "tools")
provide = ("mcp",)


def apply(ctx: PluginContext, config=None):
    if ctx.optional("mcp") is not None:
        return None
    ctx.provide("mcp", McpService())
