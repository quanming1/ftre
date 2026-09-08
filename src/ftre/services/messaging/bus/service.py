"""消息总线 Service：进程内 EventBus 与 inbound Hook 的唯一 Owner。

Service 只负责把 Channel/Agent 的发布请求交给拥有队列的 ``EventBus``；它不保存
Session 状态、不执行命令，也不决定消息如何持久化。这样 Bus 可以在组合根替换，
而上层仍依赖稳定的 ``message_bus`` key。
"""

from __future__ import annotations

import asyncio
import inspect

from ftre.kernel.hooks import HookRuntime

from ..wire import FrameBase, SessionMaintenanceFrame, SessionProjectionFrame
from .bus import EventBus
from .ingress import MESSAGING_ROUTE_SPEC, IngressResult
from .message import BusMessage


class MessageBusService:
    """暴露总线 Service，避免各 Channel 自己创建全局 Bus。"""
    key = "message_bus"

    def __init__(self, bus: EventBus | None = None, hooks: HookRuntime | None = None) -> None:
        self.bus = bus or EventBus()
        self._hooks = hooks
        self._consumer: asyncio.Task | None = None

    async def publish_inbound(self, message) -> None:
        """Publish a fire-and-forget inbound message through the owned Bus."""
        await self.bus.publish_inbound(message)

    async def publish_outbound(self, message: BusMessage) -> None:
        """Publish an existing BusMessage without exposing the underlying EventBus."""
        await self.bus.publish_outbound(message)

    async def publish_frame(
        self,
        session_id: str,
        channel_id: str,
        frame: FrameBase,
        *,
        global_broadcast: bool = False,
    ) -> None:
        """发布一个下行 Wire 帧（唯一帧铸造入口，PRD-F41 FR4/FR5）。

        帧以 ``downstream_frame`` 主题进入 Bus；ChannelManager 对该主题执行
        owner channel + ws 观察面双投。``global_broadcast=True`` 时投递给所有
        channel（会话列表级广播预留）。
        """
        await self.bus.publish_outbound(
            BusMessage(
                type="downstream_frame",
                from_channel=channel_id,
                to_channel="*" if global_broadcast else channel_id,
                from_session=session_id,
                to_session="*" if global_broadcast else session_id,
                data=frame.model_dump(mode="json"),
            )
        )

    async def publish_maintenance(
        self, session_id: str, channel_id: str, name: str, value: dict
    ) -> None:
        """便捷出口：session/maintenance 帧（指令反馈 / compaction 瞬态）。"""
        await self.publish_frame(
            session_id,
            channel_id,
            SessionMaintenanceFrame(
                session_id=session_id, payload={"name": name, "value": value}
            ),
        )

    async def publish_projection(
        self, session_id: str, channel_id: str, key: str, value, seq: int
    ) -> None:
        """便捷出口：session/projection 帧（todo/plan/title/token 快照）。"""
        await self.publish_frame(
            session_id,
            channel_id,
            SessionProjectionFrame(
                session_id=session_id,
                payload={"key": key, "value": value, "seq": seq},
            ),
        )

    async def request_inbound(self, message):
        """Submit an inbound request and wait for the AgentLoop admission ACK."""
        return await self.bus.request_inbound(message)

    def stop_inbound(self) -> None:
        """Close inbound admission during Gateway shutdown."""
        self.bus.stop_inbound()

    def start(self) -> None:
        """Start the one inbound consumer owned by MessageBus."""
        if self._consumer is None or self._consumer.done():
            self._consumer = asyncio.create_task(self._consume_inbound(), name="message-bus:inbound")

    async def close(self) -> None:
        """Stop request admission and drain the transport consumer."""
        self.bus.stop_inbound()
        consumer = self._consumer
        if consumer is not None:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            self._consumer = None

    async def _consume_inbound(self) -> None:
        """Resolve each Bus request through the Messaging-owned Hook pipeline."""
        try:
            async for message in self.bus.subscribe_inbound():
                try:
                    result = None
                    if self._hooks is not None:
                        result = self._hooks.dispatch(MESSAGING_ROUTE_SPEC, message)
                        if inspect.isawaitable(result):
                            result = await result
                    if result is None:
                        session_id = str(message.data.get("session_id") or message.from_session)
                        result = IngressResult(
                            accepted=False,
                            session_id=session_id,
                            request_id=str(message.metadata.request_id or message.id),
                            error={
                                "code": "inbound-unavailable",
                                "message": "没有启用能够处理该输入的 Plugin",
                                "retryable": False,
                            },
                        )
                    self.bus.resolve_inbound(message.id, result)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - request waiter needs a failure
                    self.bus.reject_inbound(message.id, exc)
        except asyncio.CancelledError:
            return
