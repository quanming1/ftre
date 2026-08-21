"""Provider Plugin for the channel registry backed by the message bus."""

from __future__ import annotations

from cordis import PluginContext

from .service import ChannelService

inject = ("message_bus",)
provide = ("channels",)


def apply(ctx: PluginContext, config=None):
    """Bind a ChannelManager to the injected bus and publish ``channels``."""
    if ctx.optional("channels") is not None:
        return
    from ftre.channel.manager import ChannelManager

    service = ChannelService(ChannelManager(ctx.message_bus.bus))
    ctx.provide("channels", service)
