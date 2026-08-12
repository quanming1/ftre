"""
Bus 消息定义

wire 协议契约（data/metadata 形状）在 protocol.py，本文件只定义 Bus 内部信封。
"""
import time
import uuid
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field, field_validator, model_validator

from .protocol import InboundMetadata, MessageType, coerce_inbound_metadata
from .payloads import (
    CommandMessagePayload,
    SessionMailboxSnapshotPayload,
)

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
        InboundMetadata（request_id/agent_id/agent_ref），dict 传入自动归一。
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


PayloadT = TypeVar("PayloadT", bound=BaseModel)


class TypedBusMessage(BusMessage, Generic[PayloadT]):
    """带强类型 Payload 的 Bus 信封。

    旧的 ``BusMessage`` 暂时保留给 inbound/core agent event；Gateway 自有
    session/global 事件必须使用本类的具体子类，避免再次退回裸字典。
    """

    data: PayloadT

    @model_validator(mode="after")
    def validate_payload_route(self) -> "TypedBusMessage[PayloadT]":
        """校验带 session_id 的 Payload 与 Bus 路由不能指向不同 Session。"""

        payload_session_id = getattr(self.data, "session_id", None)
        if not payload_session_id:
            return self

        routed_sessions = {
            route
            for route in (self.from_session, self.to_session)
            if route and route not in {GLOBAL_SESSION, payload_session_id}
        }
        if routed_sessions:
            raise ValueError(
                "Bus 路由 Session 与 Payload.session_id 不一致: "
                f"payload={payload_session_id!r}, routes={sorted(routed_sessions)!r}"
            )
        return self


class SessionMailboxSnapshotMessage(TypedBusMessage[SessionMailboxSnapshotPayload]):
    """SessionLane 发出的完整 mailbox 状态快照。"""

    type: Literal["session_event:mailbox_snapshot"] = "session_event:mailbox_snapshot"


class SessionCommandMessage(TypedBusMessage[CommandMessagePayload]):
    """session_event:command_message。"""

    type: Literal["session_event:command_message"] = "session_event:command_message"
