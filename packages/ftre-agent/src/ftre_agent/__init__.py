"""ftre-agent：Agent 稳定契约包。

只包含 AgentService、输入/结果模型、身份 Registry、Agent Hook 契约和跨包
配置快照；不包含 AgentLoop、LLM Client 或任何工具执行实现。`ftre_agent.plugin`
通过 Provider Plugin 唯一发布 ``agents`` Service；Runtime 由
``ftre-agent-runtime`` Package 提供，并注册到该 Service。

依赖边界：本包不依赖 ftre Host（``ftre.services.*``），可被测试替身和其他
Host 独立引用。
"""

from .config import AgentConfig, LLMConfig, build_llm_config, sanitize_agent_effort
from .contracts import (
    AgentListener,
    AgentRunResult,
    AgentRuntimeFactory,
    InboundMessage,
    RunStatus,
)
from .errors import (
    AgentServiceError,
    FactoryAlreadyRegisteredError,
    FactoryNotRegisteredError,
    FactoryRegistrationMismatchError,
    InvalidFactoryError,
    ServiceClosedError,
)
from .hooks import (
    AGENT_AFTER_RUN,
    AGENT_AFTER_RUN_SPEC,
    AGENT_BEFORE_REASONING,
    AGENT_BEFORE_REASONING_SPEC,
    AGENT_BEFORE_RUN,
    AGENT_BEFORE_RUN_SPEC,
    AGENT_RUN_ERROR,
    AGENT_RUN_ERROR_SPEC,
    AGENT_STOP_DECISION,
    AGENT_STOP_DECISION_SPEC,
    AfterRunPayload,
    AgentSubject,
    AllowRun,
    BeforeReasoningPayload,
    BeforeReasoningResult,
    BeforeRunPayload,
    ContinueTurn,
    RejectRun,
    RequestErrorPayload,
    RetryRequest,
    StopDecisionPayload,
    StopTurn,
)
from .registry import AgentRecord, AgentRegistry, HookScopeCarrier
from .service import AgentService, FactoryRegistration
from .status import AgentStatus

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
    "AfterRunPayload",
    "AgentConfig",
    "AgentListener",
    "AgentRecord",
    "AgentRegistry",
    "AgentRunResult",
    "AgentRuntimeFactory",
    "AgentService",
    "AgentServiceError",
    "AgentStatus",
    "AgentSubject",
    "AllowRun",
    "BeforeReasoningPayload",
    "BeforeReasoningResult",
    "BeforeRunPayload",
    "ContinueTurn",
    "FactoryAlreadyRegisteredError",
    "FactoryNotRegisteredError",
    "FactoryRegistration",
    "FactoryRegistrationMismatchError",
    "HookScopeCarrier",
    "InboundMessage",
    "InvalidFactoryError",
    "LLMConfig",
    "RejectRun",
    "RequestErrorPayload",
    "RetryRequest",
    "RunStatus",
    "ServiceClosedError",
    "StopDecisionPayload",
    "StopTurn",
    "build_llm_config",
    "sanitize_agent_effort",
]
