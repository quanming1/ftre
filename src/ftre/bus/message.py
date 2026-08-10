"""
Bus 消息定义

wire 协议契约（data/metadata 形状）在 protocol.py，本文件只定义 Bus 内部信封。
"""
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .protocol import InboundMetadata, MessageType, coerce_inbound_metadata

# 全局事件标记：to_channel / to_session 设为这个硬编码值时，
# 表示这是一条不针对单一 channel/session 的全局广播消息。
# ChannelManager 见到 GLOBAL_CHANNEL 会分发给所有已注册 Channel；
# 各 Channel 的 send() 见到 GLOBAL_SESSION 应扇出给自己管理的所有连接。
GLOBAL_CHANNEL = "*"
GLOBAL_SESSION = "*"


class BusMessage(BaseModel):
    """
    总线消息

    from_channel / from_session：消息来源
    to_channel / to_session：消息目标

    Inbound:  from=Channel, to=Agent   （type=user_message）
    Outbound: from=Agent, to=Channel   （type=agent_event/global_event/session_event）

    metadata 契约：
        InboundMetadata（frame_id/agent_id/agent_ref），dict 传入自动归一。
    data 契约：
        inbound  → user_message 载荷，形状见 protocol.InboundData
        outbound → 事件 dump / 包装结构，由各生产方定义
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    type: MessageType
    from_channel: str = ""
    from_session: str = ""
    to_channel: str = ""
    to_session: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    metadata: InboundMetadata = Field(default_factory=InboundMetadata)
    timestamp: float = Field(default_factory=time.time)

    @field_validator("metadata", mode="before")
    @classmethod
    def _coerce_metadata(cls, v: Any) -> InboundMetadata:
        return coerce_inbound_metadata(v)
