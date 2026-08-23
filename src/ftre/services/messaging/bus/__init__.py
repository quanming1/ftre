"""业务消息 Bus、协议模型和 MessageBusService。"""

from .bus import EventBus
from .ingress import MESSAGING_INBOUND_SPEC, IngressResult
from .message import (
    GLOBAL_CHANNEL,
    GLOBAL_SESSION,
    BusMessage,
    SessionCommandMessage,
    TypedBusMessage,
)
from .payloads import CommandMessagePayload
from .protocol import (
    AgentRef,
    InboundData,
    InboundMetadata,
    MessageType,
    OutboundMetadata,
    coerce_inbound_metadata,
)
from .service import MessageBusService

__all__ = [
    "GLOBAL_CHANNEL",
    "GLOBAL_SESSION",
    "MESSAGING_INBOUND_SPEC",
    "AgentRef",
    "BusMessage",
    "CommandMessagePayload",
    "EventBus",
    "InboundData",
    "InboundMetadata",
    "IngressResult",
    "MessageBusService",
    "MessageType",
    "OutboundMetadata",
    "SessionCommandMessage",
    "TypedBusMessage",
    "coerce_inbound_metadata",
]
