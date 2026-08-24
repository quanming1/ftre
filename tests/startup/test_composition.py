from __future__ import annotations

import pytest
from cordis import FiberState
from ftre_agent_core.tool import ToolRegistry

from ftre.app.gateway.bootstrap import start_gateway
from ftre.app.gateway.composition import build_composition
from ftre.plugins.builtin.command import CommandService
from ftre.services.config import ConfigService
from ftre.services.messaging.bus import EventBus, MessageBusService
from ftre.services.messaging.channel import ChannelService
from ftre.services.messaging.channel.manager import ChannelManager
from ftre.services.tools import ToolService


@pytest.mark.asyncio
async def test_default_composition_has_required_public_services(tmp_path) -> None:
    config = ConfigService(tmp_path / "config.json", {})
    composition = await build_composition(
        {},
        initial_services={
            "config": config,
            "sessions": object(),
            "message_bus": MessageBusService(),
            "channels": ChannelService(ChannelManager(EventBus())),
            "tools": ToolService(ToolRegistry()),
            "commands": CommandService(),
            "agent_profiles": object(),
            "agents": object(),
            "traces": object(),
        },
    )
    try:
        required = {item.id: item for item in composition.plugins.statuses() if item.required}
        assert required
        assert all(item.state is FiberState.ACTIVE for item in required.values())
        assert {"config", "filesystem", "http", "message_bus", "tools"}.issubset(
            composition.context.reflect.store
        )
        routes = composition.context.get("http").snapshot()
        assert any(route["path"] == "/api/health" for route in routes)
        paths = {route["path"] for route in routes}
        assert {"/", "/api/traces", "/api/sessions", "/api/config", "/api/cron", "/api/commands", "/api/images/{filename}", "/api/agents", "/api/skills", "/api/mcp"}.issubset(paths)
    finally:
        await composition.close()


@pytest.mark.asyncio
async def test_materialized_http_app_is_the_websocket_server_app(tmp_path) -> None:
    """Gateway 物化后的共享 App 同时承载健康路由和 WebSocket 路由。"""
    composition = await start_gateway(
        config={"sessions_dir": str(tmp_path / "sessions")},
    )
    try:
        assert composition.context.get("http").app is composition.http_app
        routes = {getattr(route, "path", None) for route in composition.http_app.routes}
        assert "/api/health" in routes
        assert "/" in routes
    finally:
        await composition.close()
