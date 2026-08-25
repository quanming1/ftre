"""LLM provider Hook contracts."""
# 中文说明：LLM Hook 合约导出：只定义调用流和失败决策协议，不创建 Provider、Client 或模型连接。

from .hooks import (
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
