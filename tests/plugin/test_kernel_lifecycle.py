from types import SimpleNamespace

import pytest
from fastapi import APIRouter
from ftre_agent_core.tool import Tool, ToolRegistry

from ftre.plugin.kernel import FtreContext, Plugin, PluginRegistry, PluginState


@pytest.mark.asyncio
async def test_effects_cleanup_in_reverse_order_and_dispose_is_idempotent():
    ctx = FtreContext()
    registry = PluginRegistry(ctx)
    cleanup_order = []

    class Effects(Plugin):
        name = "effects"

        async def setup(self, plugin_ctx, config):
            plugin_ctx.effect(lambda: cleanup_order.append(1))
            plugin_ctx.effect(lambda: cleanup_order.append(2))

    instance = await registry.register(Effects)
    await registry.unload("effects")
    await instance.dispose()
    assert cleanup_order == [2, 1]
    assert instance.state is PluginState.DISPOSED


@pytest.mark.asyncio
async def test_tool_router_hook_and_channel_registrations_are_removed():
    root = FtreContext()
    tools = ToolRegistry()
    routers = []
    channels = _Channels()
    root.provide("tool_registry", tools)
    root.provide("routers", routers)
    root.provide("channel_manager", channels)
    registry = PluginRegistry(root)

    class Capabilities(Plugin):
        name = "capabilities"
        inject = ("tool_registry", "routers", "channel_manager")

        async def setup(self, ctx, config):
            ctx.tool_registry.register(Tool(name="demo", description="demo"))
            ctx.register_router(APIRouter())
            ctx.register_channel(SimpleNamespace(channel_id="demo-channel"))
            ctx.on("demo-event", lambda: None)

    await registry.register(Capabilities)
    assert tools.has("demo")
    assert len(routers) == 1
    assert channels.get("demo-channel") is not None
    assert root.events._hooks["demo-event"]

    await registry.unload("capabilities")
    assert not tools.has("demo")
    assert routers == []
    assert channels.get("demo-channel") is None
    assert root.events._hooks["demo-event"] == []


class _Channels:
    def __init__(self):
        self.items = {}

    def register(self, channel):
        self.items[channel.channel_id] = channel

    def unregister(self, channel_id):
        return self.items.pop(channel_id, None) is not None

    def get(self, channel_id):
        return self.items.get(channel_id)
