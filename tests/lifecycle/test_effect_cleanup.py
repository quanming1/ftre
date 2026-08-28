from __future__ import annotations

import asyncio

import pytest
from cordis import Context, FiberState
from ftre_agent.tool import ToolDefinition

from ftre.plugins.builtin.command import CommandService
from ftre.services.messaging.bus import EventBus
from ftre.services.messaging.channel import ChannelService
from ftre.services.messaging.channel.manager import ChannelManager
from ftre.services.system_prompt import SystemPromptService
from ftre.services.system_prompt.types import PromptSection
from ftre.services.tools import ToolService


async def settle(steps: int = 8) -> None:
    for _ in range(steps):
        await asyncio.sleep(0)


class FakeChannel:
    channel_id = "fake"

    async def stop(self):
        self.stopped = True

    async def send(self, message):
        return None


@pytest.mark.asyncio
async def test_contributions_are_removed_in_reverse_order() -> None:
    ctx = Context()
    tools = ToolService()
    commands = CommandService()
    channels = ChannelService(ChannelManager(EventBus()))
    prompts = SystemPromptService()
    for name, value in (("tools", tools), ("commands", commands), ("channels", channels), ("system_prompt", prompts)):
        ctx.provide(name, value)

    def behavior(plugin_ctx, _config=None):
        tool_disposer = plugin_ctx.tools.register(ToolDefinition(name="cleanup", description="", func=lambda: "ok"), owner="behavior")
        plugin_ctx.effect(lambda: tool_disposer, label="tool:cleanup")
        command_disposer = plugin_ctx.commands.register("/cleanup", lambda _ctx: None)
        plugin_ctx.effect(lambda: command_disposer, label="command:cleanup")
        channel_disposer = plugin_ctx.channels.register(FakeChannel(), owner="behavior")
        plugin_ctx.effect(lambda: channel_disposer, label="channel:fake")
        prompt_disposer = plugin_ctx.system_prompt.register_section(PromptSection(name="cleanup", content="x"))
        plugin_ctx.effect(lambda: prompt_disposer, label="prompt:cleanup")

    behavior.inject = ("tools", "commands", "channels", "system_prompt")
    fiber = ctx.plugin(behavior)
    await fiber
    assert fiber.state is FiberState.ACTIVE
    assert tools.snapshot()
    assert commands.list()
    assert channels.snapshot()
    assert prompts.snapshot()
    cleanup = ctx.dispose()
    if cleanup is not None:
        await cleanup
    assert tools.snapshot() == ()
    assert commands.list() == []
    assert channels.snapshot() == ()
    assert prompts.snapshot() == ()
