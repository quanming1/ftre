from __future__ import annotations

from cordis import PluginContext

from .service import ChannelService

inject = ("message_bus",)
provide = ("channels",)


def apply(ctx: PluginContext, config=None):
    if ctx.optional("channels") is not None:
        return None
    from ftre.channel.manager import ChannelManager

    service = ChannelService(ChannelManager(ctx.message_bus.bus))
    ctx.provide("channels", service)
