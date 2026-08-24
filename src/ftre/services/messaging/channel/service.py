"""Channel Service：协议通道注册和生命周期门面。

Service 不解析 WebSocket 帧、不消费 Agent 事件；具体协议由 providers 实现，
这里仅保证注册唯一、启动/停止顺序和发送目标明确。
"""

from __future__ import annotations

from typing import Any

from .manager import ChannelManager


class ChannelService:
    """拥有 Channel 注册表，协议行为留给各 Provider。"""
    key = "channels"

    def __init__(self, manager: ChannelManager) -> None:
        self.manager = manager

    def register(self, channel: Any, owner: str = "builtin"):
        """Register one channel and return an idempotent async disposer."""
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
        """Start all registered channel providers in the manager."""
        await self.manager.start()

    async def stop_all(self) -> None:
        """Stop every channel before its owning Composition is disposed."""
        await self.manager.stop()

    async def send(self, channel_id: str, message: Any) -> None:
        """Send through a named channel, failing explicitly when it is absent."""
        channel = self.manager.get(channel_id)
        if channel is None:
            raise KeyError(channel_id)
        await channel.send(message)

    def snapshot(self) -> tuple[dict[str, str], ...]:
        """返回通道注册诊断，不暴露 manager 内部可变字典。"""
        channels = getattr(self.manager, "_channels", {})
        return tuple({"channel_id": key, "owner": "builtin", "state": "registered"} for key in channels)
