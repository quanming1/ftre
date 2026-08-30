"""Provider-neutral models for inline message extensions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class ExtensionSpan:
    """Half-open character offsets in the source message."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError("invalid extension span")


@dataclass(frozen=True, slots=True)
class ExtensionRef:
    """A parsed canonical or user-authored ``ftre://`` reference."""

    version: str
    type: str
    name: str
    args: Mapping[str, str] = field(default_factory=dict)
    raw: str = ""
    span: ExtensionSpan = field(default_factory=lambda: ExtensionSpan(0, 0))

    def __post_init__(self) -> None:
        if self.version != "v1":
            raise ValueError("unsupported extension protocol version")
        if not self.type or not self.name:
            raise ValueError("extension type and name are required")
        object.__setattr__(self, "args", MappingProxyType(dict(self.args)))


@dataclass(frozen=True, slots=True)
class ExtensionContext:
    """Runtime context supplied to an extension handler."""

    session_id: str
    agent_id: str = ""
    workspace: str = ""
    user_message_id: str = ""
    request_id: str = ""
    cancellation: Any = None


@dataclass(frozen=True, slots=True)
class ExtensionResolution:
    """Handler result; rejected references remain ordinary user text."""

    accepted: bool
    invocation_id: str
    display: Mapping[str, Any] = field(default_factory=dict)
    message: Mapping[str, Any] | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "display", MappingProxyType(dict(self.display)))
