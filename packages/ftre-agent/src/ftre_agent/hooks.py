"""ftre-agent 的公共 Hook 契约。监听器与生命周期由 Host 注入。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Protocol

from ftre_llm.contracts import LlmStreamPayload as LLMStreamPayload

from .config import AgentConfig
from .contracts import AgentRunRequest
from .tool import ToolExecutionResult


class HookMode(StrEnum):
    """Agent 契约只声明调度语义，具体监听器由宿主 HookDispatcher 管理。

    ``WATERFALL`` 是 Agent/Plugin 修改输入或结果的主模式：每个监听器必须
    显式调用 ``next_`` 才会继续默认实现。其余模式分别表达观察、并行聚合、
    串行观察和首个非空结果短路，Agent 契约不保存这些监听器的生命周期。
    """

    EMIT = "emit"
    PARALLEL = "parallel"
    SERIAL = "serial"
    BAIL = "bail"
    WATERFALL = "waterfall"


class HookFailurePolicy(StrEnum):
    """声明监听器异常是阻断当前动作，还是只记录后继续。"""

    OBSERVE = "observe"
    PROPAGATE = "propagate"


class HookScope(StrEnum):
    """Hook 的作用域提示；作用域 Context 的创建和隔离由宿主负责。"""

    GLOBAL = "global"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class HookSpec:
    """一个公开 Hook 的静态契约，不持有运行时监听器。

    ``payload_type``/``result_type`` 把 Runtime 与宿主之间的边界固定成可校验的
    类型，而 ``default`` 使 waterfall 在没有 Plugin 参与时仍然拥有确定行为。
    这也是为什么 Spec 放在无状态 Agent 契约包中，而不是放进 ftre 的运行时注册表。
    """

    name: str
    domain: str
    mode: HookMode
    failure_policy: HookFailurePolicy = HookFailurePolicy.PROPAGATE
    payload_type: type | tuple[type, ...] | None = None
    result_type: type | tuple[type, ...] | None = None
    default: Callable[..., Any] | None = None
    scope: HookScope = HookScope.GLOBAL

    def __post_init__(self) -> None:
        if not isinstance(self.mode, HookMode):
            raise TypeError("HookSpec.mode must be a HookMode")
        if not isinstance(self.failure_policy, HookFailurePolicy):
            raise TypeError("HookSpec.failure_policy must be a HookFailurePolicy")
        if not isinstance(self.scope, HookScope):
            raise TypeError("HookSpec.scope must be a HookScope")
        if not self.name or "/" not in self.name:
            raise ValueError("HookSpec.name must be a stable domain/name pair")
        if not self.domain or "/" in self.domain:
            raise ValueError("HookSpec.domain must be a simple domain name")
        if self.name.split("/", 1)[0] != self.domain:
            raise ValueError("HookSpec.name domain must match HookSpec.domain")
        if self.mode is HookMode.WATERFALL and self.default is None:
            raise ValueError("waterfall HookSpec requires an explicit default")

    def validate_payload(self, payload: Any) -> None:
        """在进入算法前拒绝错误 payload，避免 Hook 错误延迟到深层才暴露。"""

        if self.payload_type is not None and not isinstance(payload, self.payload_type):
            raise TypeError(
                f"{self.name} payload must be {self.payload_type!r}, "
                f"got {type(payload).__name__}"
            )

    def validate_result(self, result: Any) -> None:
        """在把 Hook 结果交回算法前校验协议，保证 Runtime 不消费任意对象。"""

        if self.result_type is not None and not isinstance(result, self.result_type):
            raise TypeError(
                f"{self.name} result must be {self.result_type!r}, "
                f"got {type(result).__name__}"
            )


class HookDispatcher(Protocol):
    """宿主提供的异步调度端口。

    Runtime 只依赖这一项最小协议：不创建 Dispatcher、不注册监听器，也不解释
    ``context`` 的具体类型。ftre 可以把它映射到 Cordis；其它宿主可以提供
    自己的实现而不需要让 Runtime 反向依赖 WebSocket、Session 或 Plugin。
    """

    async def dispatch(
        self,
        spec: HookSpec,
        payload: Any,
        *,
        context: object | None = None,
    ) -> Any: ...


def _readonly_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    # Hook payload 是“本次调用的快照”。复制并冻结映射，防止监听器在异步
    # waterfall 期间修改调用方仍在使用的 arguments/metadata。
    return MappingProxyType(dict(value or {}))


# ── Tool Hook contracts ────────────────────────────────────────────────

# Tool 面只保留“进入前”和“完成后”两个真正有业务语义的边界。真实 Tool
# 执行属于 Runtime 私有算法，不作为可被 Plugin 包裹的第三个协议暴露。
TOOL_BEFORE = "tool/before"
TOOL_AFTER = "tool/after"


@dataclass(frozen=True, slots=True)
class ToolCallIdentity:
    """一次 Tool 调用的稳定坐标，供 Hook、日志和 tracing 关联使用。"""

    call_id: str
    name: str
    session_id: str = ""
    turn_id: str = ""
    agent_id: str = ""
    iteration: int = 0


@dataclass(frozen=True, slots=True)
class ToolBeforePayload:
    """执行前的可修改边界：允许、拒绝或替换参数。"""

    call: ToolCallIdentity
    arguments: Mapping[str, Any]
    cancellation: asyncio.Event

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", _readonly_mapping(self.arguments))


@dataclass(frozen=True, slots=True)
class ToolAllow:
    """明确允许当前 Tool 调用继续执行的标记结果。"""


@dataclass(frozen=True, slots=True)
class ToolDeny:
    """阻止当前 Tool 调用；``reason`` 会作为结构化失败原因向上返回。"""

    reason: str


@dataclass(frozen=True, slots=True)
class ToolArguments:
    """执行前替换后的参数；原始调用对象保持不可变。"""

    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", _readonly_mapping(self.arguments))


@dataclass(frozen=True, slots=True)
class ToolAfterPayload:
    """Tool 已执行后的可替换结果边界。"""

    call: ToolCallIdentity
    arguments: Mapping[str, Any]
    result: ToolExecutionResult
    cancellation: asyncio.Event

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", _readonly_mapping(self.arguments))


async def _allow(_payload: ToolBeforePayload) -> ToolAllow:
    # 没有 Plugin 时，Tool 默认放行；安全策略由宿主通过监听器显式收紧。
    return ToolAllow()


async def _accept(payload: ToolAfterPayload) -> ToolExecutionResult:
    # after Hook 不修改结果时沿用执行器刚刚产生的快照。
    return payload.result


TOOL_BEFORE_SPEC = HookSpec(
    TOOL_BEFORE,
    "tool",
    HookMode.WATERFALL,
    payload_type=ToolBeforePayload,
    result_type=(ToolAllow, ToolDeny, ToolArguments),
    default=_allow,
    scope=HookScope.AGENT,
)
TOOL_AFTER_SPEC = HookSpec(
    TOOL_AFTER,
    "tool",
    HookMode.WATERFALL,
    payload_type=ToolAfterPayload,
    result_type=ToolExecutionResult,
    default=_accept,
    scope=HookScope.AGENT,
)


# ── LLM stream contract ────────────────────────────────────────────────

LLM_STREAM = "llm/stream"


def _readonly_sequence(value) -> tuple[Mapping[str, Any], ...]:
    return tuple(MappingProxyType(dict(item)) for item in (value or ()))


async def _stream(payload: LLMStreamPayload) -> AsyncIterator[Any]:
    # 默认直接调用底层适配器；LLM Hook 没有监听器时不增加额外协议层。
    return payload.invoke()


LLM_STREAM_SPEC = HookSpec(
    LLM_STREAM,
    "llm",
    HookMode.WATERFALL,
    payload_type=LLMStreamPayload,
    result_type=AsyncIterator,
    default=_stream,
    scope=HookScope.AGENT,
)


# ── LLM error/retry decision contract ──────────────────────────────────

LLM_ERROR = "llm/error"


@dataclass(frozen=True, slots=True)
class LLMErrorPayload:
    """一次 LLM attempt 已归一化失败后的只读快照。

    该 Hook 只发布失败事实和当前尝试坐标；Runtime 仍然拥有 RetryEvent、退避、
    消息重读和流式收尾。Plugin 不会拿到 API Key、原始异常或可变 AgentState。
    ``attempt`` 从 1 开始，``max_attempts`` 是本次 Reasoning 的硬上限。
    """

    session_id: str
    turn_id: str
    iteration: int
    model: str
    error_code: str
    error_message: str
    attempt: int
    max_attempts: int
    cancellation: asyncio.Event
    agent_id: str = ""

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ValueError("LLMErrorPayload.attempt must be positive")
        if self.max_attempts < 1:
            raise ValueError("LLMErrorPayload.max_attempts must be positive")
        if self.attempt > self.max_attempts:
            raise ValueError("LLMErrorPayload.attempt cannot exceed max_attempts")


@dataclass(frozen=True, slots=True)
class LLMErrorDecision:
    """失败策略 Plugin 对 Runtime 的最小决策。

    ``None`` 表示不干预并使用 Runtime 默认分类；``retry``/``stop`` 只改变
    下一步决策，实际重试仍由 Runtime 执行。``delay`` 是建议值，Runtime 会对它
    做非负化；未提供时沿用 Agent 的现有 retry_delay。
    """

    action: Literal["retry", "stop"]
    reason: str = ""
    delay: float | None = None

    def __post_init__(self) -> None:
        if self.action not in {"retry", "stop"}:
            raise ValueError("LLMErrorDecision.action must be retry or stop")


async def _default_llm_error(_payload: LLMErrorPayload) -> LLMErrorDecision | None:
    """没有策略 Plugin 时交回 Runtime 的原有错误分类。"""

    return None


LLM_ERROR_SPEC = HookSpec(
    LLM_ERROR,
    "llm",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.OBSERVE,
    payload_type=LLMErrorPayload,
    result_type=(LLMErrorDecision, type(None)),
    default=_default_llm_error,
    scope=HookScope.AGENT,
)


# ── Agent before-reasoning contract ────────────────────────────────────

AGENT_BEFORE_REASONING = "agent/before-reasoning"


@dataclass(frozen=True, slots=True)
class BeforeReasoningPayload:
    """一次真实 LLM Reasoning 开始前的最小运行坐标。

    Runtime 不知道消息来自 Inbox、Channel 还是其它宿主。宿主只需要根据
    ``session_id``/``turn_id``/``iteration`` 决定是否贡献上下文即可；
    ``agent`` 保留为不透明对象，避免 Runtime 反向依赖宿主 Agent 类型。
    """

    agent: object
    session_id: str
    turn_id: str
    iteration: int
    cancellation: asyncio.Event


@dataclass(frozen=True, slots=True)
class BeforeReasoningResult:
    """Hook 可追加到本次 LLM snapshot 前的结构化消息。

    这里使用 Provider 无关的 mapping，而不是 Inbox/Session 模型；Runtime 只负责
    按顺序写入自己的 AgentState，消息来源和幂等/claim 语义由宿主 Plugin 负责。
    """

    messages: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", _readonly_sequence(self.messages))


async def _before_reasoning(_payload: BeforeReasoningPayload) -> BeforeReasoningResult:
    """没有宿主监听器时保持原行为：不追加任何上下文。"""

    return BeforeReasoningResult()


AGENT_BEFORE_REASONING_SPEC = HookSpec(
    AGENT_BEFORE_REASONING,
    "agent",
    HookMode.WATERFALL,
    payload_type=BeforeReasoningPayload,
    result_type=BeforeReasoningResult,
    default=_before_reasoning,
    scope=HookScope.AGENT,
)


# ── Agent stop-decision contract ───────────────────────────────────────

AGENT_STOP_DECISION = "agent/stop-decision"


@dataclass(frozen=True, slots=True)
class StopDecisionPayload:
    """Agent 准备正常停止时的决策快照。

    只有 ``COMPLETED`` 这类自然停止才进入该 Hook；错误、取消和迭代上限等
    被迫退出不应被 Plugin 伪装成“继续工作”。
    """

    agent: object
    session_id: str
    turn_id: str
    status: str
    request_id: str
    cancellation: asyncio.Event
    last_assistant_text: str = ""
    finish_reason: str = ""
    iteration: int = 0
    continuation_count: int = 0
    max_continuations: int = 3


@dataclass(frozen=True, slots=True)
class StopTurn:
    """允许当前 Agent Turn 结束的默认结果。"""

    reason: str = ""


@dataclass(frozen=True, slots=True)
class ContinueTurn:
    """阻止本次停止并要求 Runtime 继续下一轮 Reasoning。"""

    prompt: str
    reason: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("ContinueTurn.prompt must be non-empty")


async def _stop_turn(_payload: StopDecisionPayload) -> StopTurn:
    # 无宿主策略时保持最小、可预测的自然结束语义。
    return StopTurn()


AGENT_STOP_DECISION_SPEC = HookSpec(
    AGENT_STOP_DECISION,
    "agent",
    HookMode.WATERFALL,
    payload_type=StopDecisionPayload,
    result_type=(StopTurn, ContinueTurn),
    default=_stop_turn,
    scope=HookScope.AGENT,
)




# Agent Service owns lifecycle and active-turn names; Kernel only dispatches them.
AGENT_BEFORE_RUN = "agent/before-run"
AGENT_AFTER_RUN = "agent/after-run"
AGENT_RUN_ERROR = "agent/run-error"
AGENT_BEFORE_REASONING = AGENT_BEFORE_REASONING_SPEC.name
AGENT_STOP_DECISION = AGENT_STOP_DECISION_SPEC.name


@dataclass(frozen=True, slots=True)
class AgentSubject:
    """Agent scope carrier；identity 必须跨生命周期唯一。"""

    agent_id: str
    identity: object


@dataclass(frozen=True, slots=True)
class BeforeRunPayload:
    """Agent Run 建立前的准入输入。"""

    agent: AgentSubject
    request: AgentRunRequest | None = None
    session_id: str = ""
    turn_id: str = ""
    cancellation: asyncio.Event = field(default_factory=asyncio.Event)
    channel_id: str = ""
    config: AgentConfig | None = None
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AllowRun:
    """允许本次 AgentRun 创建 active Run。"""

    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RejectRun:
    """阻止本次 AgentRun 进入 active Run。"""

    reason: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class DeferRun:
    """暂缓本次 AgentRun；队列调用方可以按 retry_at 再次尝试。"""

    reason: str
    retry_at: str | None = None


@dataclass(frozen=True, slots=True)
class AfterRunPayload:
    """Run 完成后的可等待维护边界。"""

    agent: AgentSubject
    session_id: str
    turn_id: str
    request_id: str
    status: str
    cancellation: asyncio.Event
    channel_id: str = ""
    config: AgentConfig | None = None
    set_maintenance: Callable[[bool, str], Awaitable[None]] | None = None


@dataclass(frozen=True, slots=True)
class RequestErrorPayload:
    """一次 Agent Run 失败时传给错误 Hook 的结构化上下文。"""

    agent: AgentSubject
    session_id: str
    turn_id: str
    error_code: str
    message: str
    attempt: int
    cancellation: asyncio.Event
    channel_id: str = ""
    config: AgentConfig | None = None
    request: AgentRunRequest | None = None


@dataclass(frozen=True, slots=True)
class RetryRequest:
    """错误 Hook 请求重新执行当前请求，并携带最大重试次数。"""

    reason: str
    progress_token: str
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if not self.progress_token.strip():
            raise ValueError("RetryRequest.progress_token must be non-empty")
        if self.max_attempts < 1:
            raise ValueError("RetryRequest.max_attempts must be positive")


async def _allow_run(payload: BeforeRunPayload) -> AllowRun:
    return AllowRun(payload.context)


async def _stop_on_error(payload: RequestErrorPayload) -> None:
    return None


async def _continue_after_run(payload: AfterRunPayload) -> None:
    return None


AGENT_BEFORE_RUN_SPEC = HookSpec(
    AGENT_BEFORE_RUN,
    "agent",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=BeforeRunPayload,
    result_type=(AllowRun, RejectRun, DeferRun),
    default=_allow_run,
    scope=HookScope.AGENT,
)

AGENT_AFTER_RUN_SPEC = HookSpec(
    AGENT_AFTER_RUN,
    "agent",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=AfterRunPayload,
    result_type=type(None),
    default=_continue_after_run,
    scope=HookScope.AGENT,
)

AGENT_RUN_ERROR_SPEC = HookSpec(
    AGENT_RUN_ERROR,
    "agent",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=RequestErrorPayload,
    result_type=(RetryRequest, type(None)),
    default=_stop_on_error,
    scope=HookScope.AGENT,
)

__all__ = [
    "AGENT_AFTER_RUN",
    "AGENT_AFTER_RUN_SPEC",
    "AGENT_BEFORE_REASONING",
    "AGENT_BEFORE_REASONING_SPEC",
    "AGENT_BEFORE_RUN",
    "AGENT_BEFORE_RUN_SPEC",
    "AGENT_RUN_ERROR",
    "AGENT_RUN_ERROR_SPEC",
    "AGENT_STOP_DECISION",
    "AGENT_STOP_DECISION_SPEC",
    "LLM_ERROR",
    "LLM_ERROR_SPEC",
    "LLM_STREAM",
    "LLM_STREAM_SPEC",
    "TOOL_AFTER",
    "TOOL_AFTER_SPEC",
    "AfterRunPayload",
    "AgentSubject",
    "AllowRun",
    "BeforeReasoningPayload",
    "BeforeReasoningResult",
    "BeforeRunPayload",
    "ContinueTurn",
    "DeferRun",
    "HookDispatcher",
    "HookFailurePolicy",
    "HookMode",
    "HookScope",
    "HookSpec",
    "LLMErrorDecision",
    "LLMErrorPayload",
    "LLMStreamPayload",
    "RejectRun",
    "RequestErrorPayload",
    "RetryRequest",
    "StopDecisionPayload",
    "StopTurn",
    "ToolAfterPayload",
    "ToolAllow",
    "ToolArguments",
    "ToolBeforePayload",
    "ToolCallIdentity",
    "ToolDeny",
    "ToolExecutionResult",
]
