"""
Channel 抽象基类

Channel 负责：
1. 收：从外部接收输入 → BusMessage → bus.publish_inbound()
2. 发：ChannelManager 调 send() 推给外部
"""
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ftre.services.messaging.bus import BusMessage
from ftre.services.messaging.bus.protocol import InboundData, coerce_inbound_metadata

if TYPE_CHECKING:
    from ftre.services.messaging.bus import EventBus

logger = logging.getLogger(__name__)


class Channel(ABC):
    """
    Channel 抽象基类

    一个 Channel 实例管理多个 session。
    outbound 分发由 ChannelManager 负责，Channel 只实现 send()。
    """

    def __init__(self, channel_id: str, name: str, bus: "EventBus"):
        self.channel_id = channel_id
        self.name = name
        self.bus = bus

    async def start(self) -> None:
        """启动 Channel（子类可覆盖）"""
        logger.info(f"[channel:{self.channel_id}] {self.name} started")

    async def stop(self) -> None:
        """停止 Channel（子类可覆盖）"""
        logger.info(f"[channel:{self.channel_id}] {self.name} stopped")

    async def receive(
        self,
        session_id: str,
        data: dict[str, Any] | InboundData,
        metadata: dict | None = None,
        *,
        kind: str = "user_message",
    ):
        """
        接收外部输入 → 投递到 Bus。

        子类的协议解析层（如 WebSocketChannel._on_message）应在拆完帧后
        调用这里，而不是自己构造 BusMessage 直接 publish_inbound，确保
        "外部 → Bus" 的入口唯一可控。

        边界校验（wire 协议契约见 ftre.services.messaging.bus.protocol）：
        - data     过 InboundData 归一（未知键丢弃）
        - metadata 过 InboundMetadata 归一（服务端受信来源，非白名单）

        Args:
            session_id: 目标 session
            data:       user_message 载荷（InboundData 形状）
            metadata:   附加元数据（InboundMetadata 形状；WS 会在自己的边界
                        将请求相关性透传为 request_id，AgentLoop 仅处理后者）。
                        WS 客户端帧必须先用
                        InboundMetadata.from_client 过白名单再传进来。
            kind:       BusMessage.type，通常为 "user_message"
                        （控制操作统一使用不持久化的 slash command）
        """
        msg = BusMessage(
            type=kind,
            from_channel=self.channel_id,
            from_session=session_id,
            to_channel=self.channel_id,
            to_session=session_id,
            data=InboundData.coerce(data).model_dump(),
            metadata=coerce_inbound_metadata(metadata),
        )
        # 用户输入必须等待 AgentLoop 的 durable admission ACK，不能只确认进了内存队列。
        return await self.bus.request_inbound(msg)

    @abstractmethod
    async def send(self, msg: BusMessage) -> None:
        """推送 outbound 消息给外部（子类实现）"""
        ...
