"""业务消息 Bus、协议模型和 MessageBusService。"""

from ..wire import (
    DownstreamFrame,
    FrameBase,
    RpcFrame,
    SessionEventFrame,
    SessionMaintenanceFrame,
    SessionProjectionFrame,
    SessionQueueFrame,
    SessionSubscribedFrame,
)
from .bus import EventBus
from .ingress import MESSAGING_ROUTE_SPEC, IngressResult
from .message import (
    GLOBAL_CHANNEL,
    GLOBAL_SESSION,
    BusMessage,
    TypedBusMessage,
)
from .payloads import CommandMessagePayload
from .protocol import (
    AgentRef,
    InboundData,
    InboundMetadata,
    MessageType,
    OutboundMetadata,
    PromptMode,
    coerce_inbound_metadata,
)
from .service import MessageBusService

__all__ = [
    "GLOBAL_CHANNEL",
    "GLOBAL_SESSION",
    "MESSAGING_ROUTE_SPEC",
    "AgentRef",
    "BusMessage",
    "CommandMessagePayload",
    "DownstreamFrame",
    "EventBus",
    "FrameBase",
    "InboundData",
    "InboundMetadata",
    "IngressResult",
    "MessageBusService",
    "MessageType",
    "OutboundMetadata",
    "PromptMode",
    "RpcFrame",
    "SessionEventFrame",
    "SessionMaintenanceFrame",
    "SessionProjectionFrame",
    "SessionQueueFrame",
    "SessionSubscribedFrame",
    "TypedBusMessage",
    "coerce_inbound_metadata",
]
