"""ftre 业务路径对 Core LLM Hook 契约的稳定重导出。"""

from ftre_agent_core.hooks import (
    LLM_ERROR,
    LLM_ERROR_SPEC,
    LLM_STREAM,
    LLM_STREAM_SPEC,
    LLMErrorDecision,
    LLMErrorPayload,
    LLMStreamPayload,
)

__all__ = [
    "LLM_ERROR",
    "LLM_ERROR_SPEC",
    "LLM_STREAM",
    "LLM_STREAM_SPEC",
    "LLMErrorDecision",
    "LLMErrorPayload",
    "LLMStreamPayload",
]
