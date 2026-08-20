from __future__ import annotations

from typing import Any

from ftre.channel.manager import ChannelManager


class ChannelService:
    key = "channels"

    def __init__(self, manager: ChannelManager) -> None:
        self.manager = manager

    def register(self, channel: Any, owner: str = "builtin"):
        channel_id = channel.channel_id
        if self.manager.get(channel_id) is not None:
            raise ValueError(f"channel {channel_id!r} already registered")
        self.manager.register(channel)
        disposed = False

        async def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            stop = getattr(channel, "stop", None)
            if callable(stop):
                result = stop()
                if hasattr(result, "__await__"):
                    await result
            return self.manager.unregister(channel_id)

        return dispose

    async def start_all(self) -> None:
        await self.manager.start()

    async def stop_all(self) -> None:
        await self.manager.stop()

    async def send(self, channel_id: str, message: Any) -> None:
        channel = self.manager.get(channel_id)
        if channel is None:
            raise KeyError(channel_id)
        await channel.send(message)

    def snapshot(self) -> tuple[dict[str, str], ...]:
        channels = getattr(self.manager, "_channels", {})
        return tuple({"channel_id": key, "owner": "builtin", "state": "registered"} for key in channels)

