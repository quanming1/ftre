"""Legacy Channel package surface; implementations live in ``services``."""

from ftre.services.messaging.channel.base import Channel
from ftre.services.messaging.channel.manager import ChannelManager
from ftre.services.messaging.channel.providers.subagent.channel import (
    SUBAGENT_CHANNEL_ID,
    SubagentChannel,
)
from ftre.services.messaging.channel.providers.websocket.channel import WebSocketChannel

from .test_channel import TestChannel

__all__ = [
    "SUBAGENT_CHANNEL_ID",
    "Channel",
    "ChannelManager",
    "SubagentChannel",
    "TestChannel",
    "WebSocketChannel",
]
