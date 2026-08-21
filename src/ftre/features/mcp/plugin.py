"""Feature Plugin for MCP connection state and tool registration."""

from __future__ import annotations

from cordis import PluginContext

from .service import McpService

inject = ("config", "tools")
provide = ("mcp",)


def apply(ctx: PluginContext, config=None):
    """Publish McpService; adapters and routers consume its public state."""
    if ctx.optional("mcp") is not None:
        return
    ctx.provide("mcp", McpService())
