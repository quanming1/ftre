
"""Runtime 内部的已归一化输入，不属于 AgentService 公共契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeInput:
    session_id: str
    request_id: str
    channel_id: str
    content: str = ""
    attachments: tuple[dict[str, Any], ...] = ()
    source: str = "user"
    metadata: dict[str, Any] | None = None


__all__ = ["RuntimeInput"]
