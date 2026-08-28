"""Tool 调用端口的数据模型。

这些类型不包含注册表或执行器状态；它们只描述 Runtime 与 Host ToolService
之间的一次调用和一次结果。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .definition import ToolDefinition


@dataclass(frozen=True, slots=True)
class ToolSchema:
    name: str
    description: str
    parameters: Mapping[str, Any]
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolContext:
    call_id: str
    name: str
    arguments: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    cancellation: asyncio.Event | None = None


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    id: str
    name: str
    input: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    output: str = ""
    status: str = "completed"
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    value: Any = None

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"invalid tool result status: {self.status}")


class ToolView(Protocol):
    @property
    def names(self) -> tuple[str, ...]: ...

    def schemas(self) -> tuple[ToolSchema, ...]: ...
    def get(self, name: str) -> ToolDefinition | None: ...

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolExecutionResult: ...


__all__ = [
    "ToolCallRequest",
    "ToolContext",
    "ToolExecutionResult",
    "ToolSchema",
    "ToolView",
]
