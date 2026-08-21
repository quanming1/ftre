from __future__ import annotations

import pytest

from ftre.services.messaging.bus import EventBus
from ftre.services.messaging.channel.base import Channel
from ftre.services.messaging.channel.manager import ChannelManager


class CountingChannel(Channel):
    def __init__(self, bus):
        super().__init__("counting", "counting", bus)
        self.starts = 0
        self.stops = 0

    async def start(self):
        self.starts += 1

    async def stop(self):
        self.stops += 1

    async def send(self, msg):
        return None


@pytest.mark.asyncio
async def test_channel_manager_start_stop_is_idempotent():
    manager = ChannelManager(EventBus())
    channel = CountingChannel(manager.bus)
    manager.register(channel)

    await manager.start()
    first_task = manager._dispatch_task
    await manager.start()
    assert manager._dispatch_task is first_task
    assert channel.starts == 1

    await manager.stop()
    await manager.stop()
    assert channel.stops == 1
    assert manager._dispatch_task is None
