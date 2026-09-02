"""Feature Plugin for MCP configuration, catalog and ToolView registration."""

from __future__ import annotations

from typing import Any

from cordis import Context

from .connection import McpManager
from .service import McpService

inject = ("config", "tools", "attachments", "agent_profiles", "workspaces", "http")
provide = ("mcp",)


async def apply(ctx: Context, config: dict[str, Any] | None = None):
    """Publish one MCP owner and bind it to public Host services."""
    if ctx.get("mcp", strict=False) is not None:
        return

    manager = McpManager(
        tool_service=ctx.tools,
        attachment_service=ctx.attachments,
    )
    service = McpService(
        manager,
        tool_service=ctx.tools,
        config_service=ctx.config,
        agent_profiles=ctx.agent_profiles,
        workspaces=ctx.workspaces,
    )
    ctx.provide("mcp", service)

    async def prepare_view(agent_id, session_id, profile_config, _llm_config):
        """Prepare Agent/Session MCP scopes before ToolService freezes a ToolView."""
        await service.prepare_view(agent_id, session_id, profile_config)

    view_disposer = ctx.tools.register_view_preparer(prepare_view, owner="mcp")
    ctx.effect(lambda: view_disposer, label="mcp:tool-view-preparer")

    raw = ctx.config.snapshot().value.get("mcp", {})
    await service.start_and_register(raw if isinstance(raw, dict) else {})

    async def on_config_change(snapshot) -> None:
        """ConfigService is the sole global-file watcher for MCP reloads."""
        value = getattr(snapshot, "value", {})
        next_raw = value.get("mcp", {}) if isinstance(value, dict) else {}
        await service.reload_and_register(
            next_raw if isinstance(next_raw, dict) else {},
            source=f"config:{getattr(snapshot, 'revision', 'unknown')}",
        )

    config_disposer = ctx.config.watch(on_config_change)
    ctx.effect(lambda: config_disposer, label="mcp:config-watch")
    ctx.effect(lambda: service.stop, label="mcp:stop")

    from .router import build_router

    route_disposer = ctx.http.register_router(build_router(service), owner="mcp")
    ctx.effect(lambda: route_disposer, label="http:mcp")
