from __future__ import annotations

import pytest
from cordis import Context, FiberState
from ftre_agent_core.tool import ToolRegistry

from ftre.plugins.builtin.schedule.plugin import apply, inject, provide
from ftre.services.http.service import HttpService
from ftre.services.messaging.bus import EventBus, MessageBusService
from ftre.services.messaging.channel import ChannelService
from ftre.services.messaging.channel.manager import ChannelManager
from ftre.services.tools import ToolService


class _Sessions:
    async def create_session(self, channel_id: str, title: str = "") -> str:
        return "cron_test"


async def schedule_plugin(ctx, config=None):
    return await apply(ctx, config)


schedule_plugin.inject = inject
schedule_plugin.provide = provide


@pytest.mark.asyncio
async def test_schedule_plugin_owns_channel_tool_scheduler_and_cleanup(tmp_path) -> None:
    bus = EventBus()
    channels = ChannelService(ChannelManager(bus))
    tools = ToolService(ToolRegistry())
    root = Context()
    root.provide("message_bus", MessageBusService(bus))
    root.provide("sessions", _Sessions())
    root.provide("channels", channels)
    root.provide("tools", tools)
    root.provide("http", HttpService())
    fiber = root.plugin(schedule_plugin, {"root": str(tmp_path), "scan_interval": 60})
    await fiber

    assert fiber.state is FiberState.ACTIVE
    assert root.get("schedule").list() == []
    assert channels.manager.get("cron") is not None
    assert tools.get("cron") is not None
    assert root.get("schedule") is not None

    cleanup = fiber.dispose()
    if cleanup is not None:
        await cleanup
    assert channels.manager.get("cron") is None
    assert tools.get("cron") is None
    cleanup = root.dispose()
    if cleanup is not None:
        await cleanup
