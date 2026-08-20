from __future__ import annotations

import pytest
from ftre_agent_core.tool import ToolRegistry

from cordis import FiberState
from ftre.app.gateway.composition import build_composition
from ftre.services.command import CommandService
from ftre.services.config import ConfigService
from ftre.services.messaging.bus import MessageBusService
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
            "channels": object(),
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
        assert {"config", "filesystem", "http", "message_bus", "tools"}.issubset(composition.context.services)
        routes = composition.context.get("http").snapshot()
        assert any(route["path"] == "/api/health" for route in routes)
    finally:
        await composition.close()

