"""Optional provider for the desktop WebSocket channel."""

from __future__ import annotations

from cordis import PluginContext

from .channel import WebSocketChannel

inject = ("message_bus", "channels")
provide = ()


def apply(ctx: PluginContext, config=None):
    """Register a configured WebSocket channel without opening its server here."""
    options = config if isinstance(config, dict) else {}
    channel = WebSocketChannel(ctx.message_bus.bus, host=options.get("host", "127.0.0.1"), port=int(options.get("port", 48650)))
    disposer = ctx.channels.register(channel, owner="websocket-channel")
    ctx.effect(disposer, label="channel:websocket")
