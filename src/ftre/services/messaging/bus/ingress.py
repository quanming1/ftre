"""Messaging inbound Hook contract.

MessageBus owns transport correlation and publishes one normalized inbound
boundary. Command and Inbox Plugins decide whether they handle a message;
Agent Runtime does not inspect either product concept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ftre.kernel.hooks import HookFailurePolicy, HookMode, HookScope, HookSpec

from .message import BusMessage


@dataclass(frozen=True, slots=True)
class IngressResult:
    """The existing admission ACK shape, moved to the Messaging Owner."""

    accepted: bool
    session_id: str
    request_id: str = ""
    created: bool = False
    error: dict[str, Any] | None = None


async def _unhandled(_message: BusMessage) -> IngressResult | None:
    return None


MESSAGING_INBOUND_SPEC = HookSpec(
    "messaging/inbound",
    "messaging",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=BusMessage,
    result_type=IngressResult | type(None),
    default=_unhandled,
    scope=HookScope.GLOBAL,
)


__all__ = ["MESSAGING_INBOUND_SPEC", "IngressResult"]
