"""Tool 执行前的审批服务端口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ApprovalOutcome(StrEnum):
    ALLOWED = "allowed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    outcome: ApprovalOutcome
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ApprovalService:
    """可注入审批处理器；没有 UI 处理器时安全失败。"""

    key = "approval"

    def __init__(self, handler=None) -> None:
        self._handler = handler

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        if self._handler is None:
            return ApprovalDecision(
                ApprovalOutcome.UNAVAILABLE,
                "approval handler is unavailable",
            )
        result = self._handler(request)
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, ApprovalDecision):
            raise TypeError("Approval handler must return ApprovalDecision")
        return result


__all__ = [
    "ApprovalDecision",
    "ApprovalOutcome",
    "ApprovalRequest",
    "ApprovalService",
]
