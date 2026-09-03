"""Agent Service 的公开契约。

这些模型是 Gateway、HTTP、Channel、Inbox 和其他 Feature 可依赖的稳定边界：
``AgentRunRequest`` 是执行输入，``AgentRunResult`` 是执行结果。它们
不暴露 Inbox 队列、Session Repository、TurnExecutor 或 AgentLoop 对象。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .config import AgentConfig
from .message import Msg

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
    config: AgentConfig
    session_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentResumeSpec:
    """从已有 Session/Run 状态恢复 Agent。"""

    agent_id: str
    session_id: str
    config: AgentConfig
    checkpoint_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    """Agent Service 的目标数据面输入；不携带 Inbox/Channel 对象。"""

    session_id: str
    request_id: str
    messages: tuple[Msg, ...]
    agent_id: str | None = None
    channel_id: str = ""
    source: str = "user"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    options: RunOptions = field(default_factory=RunOptions)

    @property
    def content(self) -> str:
        return "\n".join(
            text for text in (message.get_text_content() or "" for message in self.messages) if text
        )

    @property
    def attachments(self) -> tuple[dict[str, Any], ...]:
        raw = self.metadata.get("attachments", ())
        return tuple(dict(item) for item in raw if isinstance(item, Mapping))


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
class RunReservation:
    """AgentService 为一次 Inbox 投递保留的短生命周期执行权。"""

    reservation_id: str
    agent_id: str
    session_id: str
    request_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AgentStreamEnvelope:
    """Runtime 真实事件的有序信封；Host 负责 JSON 序列化。"""

    agent_id: str
    run_id: str
    sequence: int
    event: Any


class AgentRuntimeHandle(Protocol):
    """单个 Agent 的 Runtime 操作句柄。"""

    async def run(self, request: AgentRunRequest) -> Any: ...
    async def stream(self, request: AgentRunRequest) -> AsyncIterator[AgentStreamEnvelope]: ...
    async def cancel(self, reason: str) -> Any: ...
    async def dispose(self) -> None: ...


class RuntimeControlPort(Protocol):
    """Host-control operations kept outside the Agent data-plane factory."""

    async def cancel_session(self, session_id: str, **kwargs: Any) -> Any: ...
    def get_session_status(self, session_id: str) -> str: ...
    def is_active_session(self, session_id: str) -> bool: ...
    async def delete_session(self, session_id: str) -> Any: ...
    async def resume_confirmation(self, *args: Any, **kwargs: Any) -> Any: ...


class AgentHandle(Protocol):
    """AgentService 返回给调用方的窄操作句柄。"""

    @property
    def id(self) -> str: ...

    def view(self) -> AgentView: ...
    def status(self) -> str: ...
    async def run(self, request: AgentRunRequest) -> Any: ...
    async def stream(self, request: AgentRunRequest) -> AsyncIterator[AgentStreamEnvelope]: ...
    async def cancel(self, reason: str = "") -> Any: ...
    async def dispose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """一次 Agent Run 的稳定公开结果。

    正常结束、取消和失败必须可区分；``status`` 只有三个稳定值
    （completed/cancelled/failed），Runtime 内部的中间状态不进入该契约。
    ``paused`` 是成功建立权限挂起边界的正交标记；它不代表自然完成，
    Inbox 不得在该标记下消费下一条输入。
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
    paused: bool = False


AgentListener = Callable[[dict[str, Any]], Any]


class AgentRuntimeFactory(Protocol):
    """AgentService 可消费的唯一 Runtime 数据面协议。

    Runtime 的正式数据面使用 AgentCreateSpec/AgentRunRequest。
    """

    name: str
    version: str
    control: RuntimeControlPort

    async def create(self, spec: AgentCreateSpec) -> AgentRuntimeHandle: ...
    async def resume(self, spec: AgentResumeSpec) -> AgentRuntimeHandle: ...

__all__ = [
    "AgentCreateSpec",
    "AgentHandle",
    "AgentListener",
    "AgentResumeSpec",
    "AgentRunRequest",
    "AgentRunResult",
    "AgentRuntimeFactory",
    "AgentRuntimeHandle",
    "AgentStreamEnvelope",
    "AgentView",
    "RunOptions",
    "RunReservation",
    "RunStatus",
    "RuntimeControlPort",
]
