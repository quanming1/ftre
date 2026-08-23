"""Command Service 的最小公开协议。

Command 只描述命令本身的输入和结果；Agent 恢复、Prompt 改写等数据面行为不属于
CommandResult，必须复用已有 Session Event/Agent 数据面流程。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ftre.services.messaging.bus import BusMessage


@dataclass(frozen=True)
class CommandResult:
    """一次命令的直接结果，不携带 Agent 调度指令。"""

    kind: Literal["success", "error"] = "success"
    text: str = ""
    source_event_seq: int | None = None

    @classmethod
    def success(cls, text: str = "", *, source_event_seq: int | None = None) -> CommandResult:
        return cls("success", text, source_event_seq)

    @classmethod
    def error(cls, text: str) -> CommandResult:
        if not text.strip():
            raise ValueError("command error text must not be empty")
        return cls("error", text)


@dataclass
class CommandDef:
    """命令注册元数据和 Handler。"""

    command: str
    description: str = ""
    args_hint: str = ""
    system: bool = False
    persist_input: bool = True
    sub_commands: list[CommandDef] = field(default_factory=list)
    source: str = "builtin"
    handler: Handler | None = None


@dataclass(frozen=True)
class CommandContext:
    """Handler 的显式输入；不再通过 ``meta: Any`` 反查 BusMessage。"""

    raw: str
    command: str
    args: str | None
    inbound: BusMessage

    @property
    def session_id(self) -> str:
        return self.inbound.data.get("session_id") or self.inbound.from_session

    @property
    def channel_id(self) -> str:
        return self.inbound.from_channel

    @property
    def request_id(self) -> str:
        return self.inbound.metadata.request_id


Handler = Callable[
    [CommandContext],
    CommandResult | Awaitable[CommandResult] | None,
]

__all__ = ["CommandContext", "CommandDef", "CommandResult", "Handler"]
