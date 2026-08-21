"""Optional provider that contributes the internal sub-agent channel."""

from __future__ import annotations

from cordis import Context

from .channel import SubagentChannel

inject = ("message_bus", "channels")
provide = ()


def apply(ctx: Context, config=None):
    """Register the channel and attach its disposer to this Fiber."""
    channel = SubagentChannel(ctx.message_bus.bus)
    disposer = ctx.channels.register(channel, owner="subagent-channel")
    ctx.effect(lambda: disposer, label="channel:subagent")
