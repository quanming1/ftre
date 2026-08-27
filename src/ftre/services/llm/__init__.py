"""Ftre LLM Service 与其稳定 Hook 合约。"""

from ftre_llm import (
    AdaptersUpdatedPayload,
    BlockAssembler,
    BlockEnd,
    BlockStart,
    FinishChunk,
    FinishReason,
    LlmAdapter,
    LlmCallConfig,
    LlmCredentials,
    LLMError,
    LlmRequest,
    LlmService,
    ReasoningDeltaChunk,
    StreamChunk,
    TextDeltaChunk,
    ToolCallDeltaChunk,
    UsageChunk,
)

from .hooks import (
    ADAPTERS_UPDATED_SPEC,
    AGENT_REQUEST_SPEC,
    LLM_STREAM_SPEC,
    AgentRequestPayload,
    LlmStreamPayload,
)

__all__ = [
    "ADAPTERS_UPDATED_SPEC",
    "AGENT_REQUEST_SPEC",
    "LLM_STREAM_SPEC",
    "AdaptersUpdatedPayload",
    "AgentRequestPayload",
    "BlockAssembler",
    "BlockEnd",
    "BlockStart",
    "FinishChunk",
    "FinishReason",
    "LLMError",
    "LlmAdapter",
    "LlmCallConfig",
    "LlmCredentials",
    "LlmRequest",
    "LlmService",
    "LlmStreamPayload",
    "ReasoningDeltaChunk",
    "StreamChunk",
    "TextDeltaChunk",
    "ToolCallDeltaChunk",
    "UsageChunk",
]
