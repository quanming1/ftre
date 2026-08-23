"""ftre 业务路径对 Core LLM stream Hook 契约的稳定重导出。"""

from ftre_agent_core.hooks import LLM_STREAM_SPEC, LLMStreamPayload

LLM_STREAM = LLM_STREAM_SPEC.name

__all__ = ["LLM_STREAM", "LLM_STREAM_SPEC", "LLMStreamPayload"]
