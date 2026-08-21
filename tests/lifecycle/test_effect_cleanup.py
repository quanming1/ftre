from __future__ import annotations

import pytest
from ftre_agent_core.tool import Tool, ToolRegistry

from cordis import Context, FiberState
from ftre.services.messaging.bus import EventBus
from ftre.services.messaging.channel.manager import ChannelManager
from ftre.services.command import CommandService
from ftre.services.messaging.channel import ChannelService
from ftre.services.system_prompt import SystemPromptService
from ftre.services.system_prompt.types import PromptSection
from ftre.services.tools import ToolService


class FakeChannel:
    channel_id = "fake"

    async def stop(self):
        self.stopped = True

    async def send(self, message):
        return None


@pytest.mark.asyncio
async def test_contributions_are_removed_in_reverse_order() -> None:
    ctx = Context()
    tools = ToolService(ToolRegistry())
    commands = CommandService()
    channels = ChannelService(ChannelManager(EventBus()))
    prompts = SystemPromptService()
    for name, value in (("tools", tools), ("commands", commands), ("channels", channels), ("system_prompt", prompts)):
        ctx.provide(name, value)

    class Behavior:
        inject = ("tools", "commands", "channels", "system_prompt")
        provide = ()

        def apply(self, plugin_ctx):
            tool_disposer = plugin_ctx.tools.register(Tool(name="cleanup", description="", func=lambda: "ok"), owner="behavior")
            plugin_ctx.effect(tool_disposer, label="tool:cleanup")
            command_disposer = plugin_ctx.commands.register("/cleanup", lambda _ctx: None)
            plugin_ctx.effect(command_disposer, label="command:cleanup")
            channel_disposer = plugin_ctx.channels.register(FakeChannel(), owner="behavior")
            plugin_ctx.effect(channel_disposer, label="channel:fake")
            prompt_disposer = plugin_ctx.system_prompt.register_section(PromptSection(name="cleanup", content="x"))
            plugin_ctx.effect(prompt_disposer, label="prompt:cleanup")

    fiber = ctx.plugin(Behavior, id="behavior")
    await ctx.settle()
    assert fiber.state is FiberState.ACTIVE
    assert tools.snapshot()
    assert commands.list()
    assert channels.snapshot()
    assert prompts.snapshot()
    await ctx.dispose()
    assert tools.snapshot() == ()
    assert commands.list() == []
    assert channels.snapshot() == ()
    assert prompts.snapshot() == ()
