"""Agent Service 的公开契约。

这些模型是 Gateway、HTTP、Channel、Inbox 和其他 Feature 可依赖的稳定边界：
``InboundMessage`` 是唯一执行输入，``AgentRunResult`` 是唯一执行结果。它们
不暴露 Inbox 队列、Session Repository、TurnExecutor 或 AgentLoop 对象。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ftre_agent_core.message import Msg

RunStatus = str


@dataclass(frozen=True, slots=True)
class RunOptions:
    """一次 Agent Run 的非身份选项。"""

    max_iterations: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentCreateSpec:
    """创建 Agent 所需的稳定配置快照引用。"""

    agent_id: str
    config: Any
    session_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentResumeSpec:
    """从已有 Session/Run 状态恢复 Agent。"""

    agent_id: str
    session_id: str
    config: Any
    checkpoint_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    """Agent Service 的目标数据面输入；不携带 Inbox/Channel 对象。"""

    session_id: str
    request_id: str
    messages: tuple[Msg, ...]
    channel_id: str = ""
    source: str = "user"
    metadata: Mapping[str, Any] = ()
    options: RunOptions = field(default_factory=RunOptions)


@dataclass(frozen=True, slots=True)
class AgentView:
    """AgentService 对外返回的只读诊断视图。"""

    agent_id: str
    state: str
    session_id: str | None = None
    run_id: str | None = None
    created_at: datetime | None = None
    config_snapshot_hash: str | None = None


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Agent Service 流式出口的通用事件信封。"""

    event_type: str
    agent_id: str
    run_id: str
    sequence: int
    data: Mapping[str, Any] = field(default_factory=dict)


class AgentRuntimeHandle(Protocol):
    """单个 Agent 的 Runtime 操作句柄。"""

    async def run(self, request: AgentRunRequest) -> Any: ...
    async def stream(self, request: AgentRunRequest) -> AsyncIterator[AgentEvent]: ...
    async def cancel(self, reason: str) -> Any: ...
    async def dispose(self) -> None: ...


class AgentHandle(Protocol):
    """AgentService 返回给调用方的窄操作句柄。"""

    @property
    def id(self) -> str: ...

    def view(self) -> AgentView: ...
    def status(self) -> str: ...
    async def run(self, request: AgentRunRequest) -> Any: ...
    async def stream(self, request: AgentRunRequest) -> AsyncIterator[AgentEvent]: ...
    async def cancel(self, reason: str = "") -> Any: ...
    async def dispose(self) -> None: ...


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
    run_id: str = ""
    usage: Mapping[str, Any] | None = None


AgentListener = Callable[[dict[str, Any]], Any]


class AgentRuntimeFactory(Protocol):
    """AgentService 可消费的唯一 Runtime 数据面协议。

    F35.1 仍保留现有 InboundMessage 数据面；F35.2 会把这些方法收敛为
    AgentCreateSpec/AgentRunRequest。这里先用独立 Protocol 约束 Service 与
    Runtime 的所有权方向，禁止把具体 AgentLoop 当作公共 Service。
    """

    name: str
    version: str

    async def create(self, spec: AgentCreateSpec) -> AgentRuntimeHandle: ...
    async def resume(self, spec: AgentResumeSpec) -> AgentRuntimeHandle: ...

    def run_inbound(self, message: InboundMessage) -> Any: ...
    def cancel_session(self, *args: Any, **kwargs: Any) -> Any: ...
    def get_session_status(self, session_id: str) -> str: ...
    def is_active_session(self, session_id: str) -> bool: ...
    def delete_session(self, session_id: str) -> Any: ...
    def resume_confirmation(self, *args: Any, **kwargs: Any) -> Any: ...

__all__ = [
    "AgentCreateSpec",
    "AgentEvent",
    "AgentHandle",
    "AgentListener",
    "AgentResumeSpec",
    "AgentRunRequest",
    "AgentRunResult",
    "AgentRuntimeFactory",
    "AgentRuntimeHandle",
    "AgentView",
    "InboundMessage",
    "RunOptions",
    "RunStatus",
]
