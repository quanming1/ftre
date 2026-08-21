"""Optional provider for the desktop WebSocket channel."""

from __future__ import annotations

from cordis import Context

from .channel import WebSocketChannel

inject = ("message_bus", "channels", "attachments")
provide = ()


def apply(ctx: Context, config=None):
    """Register a configured WebSocket channel without opening its server here."""
    options = config if isinstance(config, dict) else {}
    channel = WebSocketChannel(
        ctx.message_bus.bus,
        host=options.get("host", "127.0.0.1"),
        port=int(options.get("port", 48650)),
        attachment_service=ctx.attachments,
    )
    disposer = ctx.channels.register(channel, owner="websocket-channel")
    ctx.effect(lambda: disposer, label="channel:websocket")
