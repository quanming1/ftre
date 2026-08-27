"""Agent Service 的公开契约。

这些模型是 Gateway、HTTP、Channel、Inbox 和其他 Feature 可依赖的稳定边界：
``InboundMessage`` 是唯一执行输入，``AgentRunResult`` 是唯一执行结果。它们
不暴露 Inbox 队列、Session Repository、TurnExecutor 或 AgentLoop 对象。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

RunStatus = str


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """已经归一化、准备交给 AgentService 执行的一条输入。

    这是 AgentService 的唯一数据面输入。它不包含队列条目、pending、容量或
    客户端队列状态；Inbox Package 负责决定何时生成它。
    """

    session_id: str
    request_id: str
    channel_id: str
    content: str = ""
    attachments: tuple[dict[str, Any], ...] = ()
    source: str = "user"
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """一次 Agent Run 的稳定公开结果。

    正常结束、取消和失败必须可区分；``status`` 只有三个稳定值
    （completed/cancelled/failed），Runtime 内部的中间状态不进入该契约。
    ``error`` 仅在失败时携带结构化上下文，不包含凭据或 Prompt 全文。
    """

    session_id: str
    turn_id: str
    status: RunStatus
    user_message_id: str = ""
    final_content: str = ""
    error: Mapping[str, Any] | None = None


AgentListener = Callable[[dict[str, Any]], Any]


class AgentRuntimeFactory(Protocol):
    """AgentService 可消费的唯一 Runtime 数据面协议。

    F35.1 仍保留现有 InboundMessage 数据面；F35.2 会把这些方法收敛为
    AgentCreateSpec/AgentRunRequest。这里先用独立 Protocol 约束 Service 与
    Runtime 的所有权方向，禁止把具体 AgentLoop 当作公共 Service。
    """

    name: str
    version: str

    def run_inbound(self, message: InboundMessage) -> Any: ...
    def cancel_session(self, *args: Any, **kwargs: Any) -> Any: ...
    def get_session_status(self, session_id: str) -> str: ...
    def is_active_session(self, session_id: str) -> bool: ...
    def delete_session(self, session_id: str) -> Any: ...
    def resume_confirmation(self, *args: Any, **kwargs: Any) -> Any: ...

__all__ = [
    "AgentListener",
    "AgentRunResult",
    "AgentRuntimeFactory",
    "InboundMessage",
    "RunStatus",
]
