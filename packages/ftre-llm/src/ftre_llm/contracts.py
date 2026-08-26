"""LLM Service 的稳定、不可变公开契约。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol

from .events import StreamChunk


@dataclass(frozen=True, slots=True)
class LlmCredentials:
    """一次调用使用的凭据；不进入 LlmCallConfig 或 Hook payload。"""

    api_key: str
    api_base: str = ""


@dataclass(frozen=True, slots=True)
class LlmMessage:
    """Provider 无关的标准消息；extra 保留协议需要的工具/Responses 字段。"""

    role: str
    content: Any = ""
    name: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))

    @classmethod
    def from_value(cls, value: LlmMessage | Mapping[str, Any]) -> LlmMessage:
        if isinstance(value, cls):
            return value
        data = dict(value)
        role = str(data.pop("role", "user"))
        content = data.pop("content", "")
        name = data.pop("name", None)
        return cls(role=role, content=content, name=name, extra=data)

    def to_mapping(self) -> dict[str, Any]:
        result = dict(self.extra)
        result["role"] = self.role
        result["content"] = self.content
        if self.name is not None:
            result["name"] = self.name
        return result


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """Provider 无关的 function tool schema。"""

    name: str
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    type: str = "function"
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))

    @classmethod
    def from_value(cls, value: ToolSchema | Mapping[str, Any]) -> ToolSchema:
        if isinstance(value, cls):
            return value
        data = dict(value)
        function = data.pop("function", None)
        if isinstance(function, Mapping):
            data = {**data, **function}
        return cls(
            name=str(data.pop("name", "")),
            description=str(data.pop("description", "")),
            parameters=data.pop("parameters", {}) or {},
            type=str(data.pop("type", "function")),
            extra=data,
        )

    def to_mapping(self) -> dict[str, Any]:
        function = {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
        }
        result = dict(self.extra)
        result["type"] = self.type
        result["function"] = function
        return result


@dataclass(frozen=True, slots=True)
class LlmCallConfig:
    """会话/轮次的模型选择与采样配置，不含消息和凭据。"""

    provider: str
    model: str
    api_type: str | None = None
    reasoning_effort: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    stop: tuple[str, ...] = ()
    timeout: float = 120.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "stop", tuple(self.stop))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class LlmRequest:
    """完整的一次调用快照；构造时复制并冻结消息和工具。"""

    config: LlmCallConfig
    messages: tuple[LlmMessage, ...]
    system: str | None = None
    tools: tuple[ToolSchema, ...] = ()
    session_id: str = ""
    turn_id: str = ""
    purpose: Literal["conversation", "compaction", "session-title"] = "conversation"
    cancellation: asyncio.Event | None = None
    agent_id: str = ""
    attempt: int = 1
    max_attempts: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "messages",
            tuple(LlmMessage.from_value(message) for message in self.messages),
        )
        object.__setattr__(
            self,
            "tools",
            tuple(ToolSchema.from_value(tool) for tool in self.tools),
        )
        if self.attempt < 1 or self.max_attempts < self.attempt:
            raise ValueError("invalid LlmRequest attempt coordinates")

    @classmethod
    def from_parts(
        cls,
        config: LlmCallConfig,
        messages: Sequence[LlmMessage | Mapping[str, Any]],
        tools: Sequence[ToolSchema | Mapping[str, Any]] | None = None,
        *,
        system: str | None = None,
        **kwargs: Any,
    ) -> LlmRequest:
        return cls(
            config=config,
            messages=tuple(LlmMessage.from_value(message) for message in messages),
            tools=tuple(ToolSchema.from_value(tool) for tool in (tools or ())),
            system=system,
            **kwargs,
        )

    def wire_messages(self) -> list[dict[str, Any]]:
        messages = [message.to_mapping() for message in self.messages]
        if self.system:
            messages.insert(0, {"role": "system", "content": self.system})
        return messages


@dataclass(frozen=True, slots=True)
class ModelInfo:
    provider: str
    model: str
    api_type: str
    context_window: int | None = None
    max_output: int | None = None
    vision: bool = False
    reasoning_effort_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    name: str
    api_type: str
    models: tuple[ModelInfo, ...] = ()


class LlmAdapter(ABC):
    """所有协议适配器唯一的运行时契约。

    ``LlmService`` 只接收这个契约，不认识 OpenAI、Responses 或 Completions。
    具体协议的共享骨架放在 ``base.py``，但不再重复声明第二个 Adapter 类型；
    这样 Provider Plugin 注册的每个工厂都落在同一个可检查的扩展边界上。
    """

    @abstractmethod
    def stream(
        self,
        request: LlmRequest,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """执行一次调用并产出完整的 StreamChunk 流。"""

    @abstractmethod
    def cancel(self) -> None:
        """请求取消；适配器应在下一个安全检查点结束流。"""

    async def resolve_model_info(self, model: str) -> ModelInfo | None:
        """返回模型能力；无法查询远端目录的适配器可以返回 ``None``。"""

        del model
        return None

    def retry_policy(self) -> Any:
        """返回适配器级提示；重试决策仍归独立 Plugin。"""

        return None


AdapterFactory = Callable[..., LlmAdapter]


class AdapterRegistration(Protocol):
    """可逆的 Provider 注册句柄。"""

    def dispose(self) -> bool: ...

    def replace(self, providers: Sequence[str]) -> bool: ...


class PreparedLlmCall:
    """捕获 Adapter registration 的单次调用句柄。"""

    def __init__(
        self,
        config: LlmCallConfig,
        adapter: LlmAdapter,
        *,
        model_info: ModelInfo | None = None,
        retry_policy: Any = None,
        adapter_defaults: frozenset[str] = frozenset(),
        on_complete=None,
    ) -> None:
        self.config = config
        self.model_info = model_info
        self.retry_policy = retry_policy
        self.adapter_defaults = adapter_defaults
        self._adapter = adapter
        self._on_complete = on_complete
        self._used = False

    async def stream(self, request: LlmRequest) -> AsyncIterator[StreamChunk]:
        if self._used:
            raise RuntimeError("PreparedLlmCall.stream() can only be called once")
        if request.config != self.config:
            # prepare_call 会补齐 api_type/max_tokens 等默认值；调用方持有的
            # 原始 Request 可能还未带这些派生字段，因此在句柄边界统一配置。
            request = replace_request_config(request, self.config)
        self._used = True
        try:
            async for chunk in self._adapter.stream(request):
                yield chunk
        finally:
            if self._on_complete is not None:
                self._on_complete(self)

    def cancel(self) -> None:
        self._adapter.cancel()


def replace_request_config(request: LlmRequest, config: LlmCallConfig) -> LlmRequest:
    """复制 Request 并替换规范化后的调用配置。"""

    return LlmRequest(
        config=config,
        messages=request.messages,
        system=request.system,
        tools=request.tools,
        session_id=request.session_id,
        turn_id=request.turn_id,
        purpose=request.purpose,
        cancellation=request.cancellation,
        agent_id=request.agent_id,
        attempt=request.attempt,
        max_attempts=request.max_attempts,
    )


@dataclass(frozen=True, slots=True)
class LlmStreamPayload:
    """一次 LLM 流的只读 Hook 快照。"""

    agent_id: str
    session_id: str
    turn_id: str
    model: str
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...]
    cancellation: asyncio.Event
    attempt: int = 1
    max_attempts: int = 1
    invoke: Callable[[], AsyncIterator[StreamChunk]] | None = None
    # Core Runner 兼容字段：Provider 和调用目的由 Host 传入；直接构造时可省略。
    provider: str = ""
    purpose: str = "conversation"

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(MappingProxyType(dict(item)) for item in self.messages))
        object.__setattr__(self, "tools", tuple(MappingProxyType(dict(item)) for item in self.tools))
        if self.attempt < 1:
            raise ValueError("LLMStreamPayload.attempt must be positive")
        if self.max_attempts < 1:
            raise ValueError("LLMStreamPayload.max_attempts must be positive")
        if self.attempt > self.max_attempts:
            raise ValueError("LLMStreamPayload.attempt cannot exceed max_attempts")


@dataclass(frozen=True, slots=True)
class AgentRequestPayload:
    agent_id: str
    session_id: str
    turn_id: str
    step: int
    config: LlmCallConfig
    previous_failure: Any
    cancellation: asyncio.Event


@dataclass(frozen=True, slots=True)
class AdaptersUpdatedPayload:
    """适配器路由变化通知；只暴露稳定的 Provider 名称。"""

    providers: tuple[str, ...]
    operation: Literal["register", "replace", "dispose"] = "register"
