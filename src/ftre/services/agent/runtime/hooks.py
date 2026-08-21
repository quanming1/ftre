"""Agent runtime hook contracts independent of the legacy Plugin Kernel."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BEFORE_MESSAGES_BUILD = "agent/before_messages_build"
BEFORE_AGENT_RUN = "agent/before_run"


@dataclass
class MessagesBuildContext:
    """Mutable input for the ``agent/before_messages_build`` filter."""

    session_id: str
    channel_id: str
    inbound_data: dict
    workspace: str
    reply_id: str = ""
    agent_dir: str = ""
    event_loop: Any = None
    config: Any = None
    messages: list = field(default_factory=list)


@dataclass
class AgentRunContext:
    """Mutable input for the ``agent/before_run`` filter."""

    session_id: str
    channel_id: str
    messages: list[dict]
    config: Any
    agent_profile: Any = None
    agent_tool_registry: Any = None
    workspace: str = ""


def append_to_first_system(messages: list[dict], text: str) -> None:
    """Append text to the first system message, creating one when necessary."""
    text = (text or "").strip()
    if not text:
        return
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            current = (message.get("content") or "").rstrip()
            message["content"] = f"{current}\n\n{text}" if current else text
            return
    messages.insert(0, {"role": "system", "content": text})


__all__ = [
    "BEFORE_AGENT_RUN",
    "BEFORE_MESSAGES_BUILD",
    "AgentRunContext",
    "MessagesBuildContext",
    "append_to_first_system",
]
