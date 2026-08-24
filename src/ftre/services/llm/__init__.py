"""LLM provider Hook contracts."""
# 中文说明：LLM Hook 合约导出：只定义 stream 观察协议，不创建 Provider、Client 或模型连接。

from .hooks import LLM_STREAM_SPEC, LLMStreamPayload

__all__ = ["LLM_STREAM_SPEC", "LLMStreamPayload"]
