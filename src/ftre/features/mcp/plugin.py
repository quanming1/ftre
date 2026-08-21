"""Feature Plugin for MCP connection state and tool registration."""

from __future__ import annotations

from cordis import PluginContext

from .connection import McpManager
from .service import McpService

inject = ("config", "tools")
provide = ("mcp",)


async def apply(ctx: PluginContext, config=None):
    """Publish MCP state and own the transport manager's full lifecycle."""
    if ctx.optional("mcp") is not None:
        return
    manager = McpManager(tool_registry=ctx.tools.registry)
    service = McpService(manager)
    ctx.provide("mcp", service)
    raw = ctx.config.snapshot().value.get("mcp", {})
    if isinstance(raw, dict) and raw:
        await service.start_and_register(raw)
    ctx.effect(service.stop, label="mcp:stop")
