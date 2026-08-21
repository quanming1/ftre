"""Optional provider that contributes the internal sub-agent channel."""

from __future__ import annotations

from cordis import PluginContext
from ftre.channel.subagent_channel import SubagentChannel

inject = ("message_bus", "channels")
provide = ()


def apply(ctx: PluginContext, config=None):
    """Register the channel and attach its disposer to this Fiber."""
    channel = SubagentChannel(ctx.message_bus.bus)
    disposer = ctx.channels.register(channel, owner="subagent-channel")
    ctx.effect(disposer, label="channel:subagent")
