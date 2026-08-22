"""Feature Plugin for MCP connection state and tool registration."""

from __future__ import annotations

from cordis import Context

from .connection import McpManager
from .service import McpService

inject = ("config", "tools", "attachments")
provide = ("mcp",)


async def apply(ctx: Context, config=None):
    """Publish MCP state and own the transport manager's full lifecycle."""
    if ctx.get("mcp", strict=False) is not None:
        return
    manager = McpManager(
        tool_service=ctx.tools,
        attachment_service=ctx.attachments,
    )
    service = McpService(manager, tool_service=ctx.tools)
    ctx.provide("mcp", service)
    raw = ctx.config.snapshot().value.get("mcp", {})
    if isinstance(raw, dict) and raw:
        await service.start_and_register(raw)
    ctx.effect(lambda: service.stop, label="mcp:stop")
