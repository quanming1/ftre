"""Host LLM Service 的调用 Hook。

这里故意不声明 LLM 失败/重试 Hook：实际 Agent Turn 的错误裁决由 Runtime 的
``llm/error`` 统一发布并消费。LlmService 只把失败作为 ``FinishChunk`` 返回，
避免再建立一条“返回值被忽略”的平行重试链。

``llm/stream`` 也直接重导出 Agent 契约包的 Spec。Runtime 与 LlmService 内部
因此命中同一个 Hook 合约，Fallback Plugin 不会因为两份同名 Spec 触发冲突。
"""

from __future__ import annotations

from ftre_agent.hooks import LLM_STREAM, LLM_STREAM_SPEC
from ftre_agent.hooks import LLMStreamPayload as LlmStreamPayload
from ftre_llm.contracts import (
    AdaptersUpdatedPayload,
    AgentRequestPayload,
)

from ftre.kernel.hooks import HookFailurePolicy, HookMode, HookScope, HookSpec

AGENT_REQUEST = "agent/request"
LLM_ADAPTERS_UPDATED = "llm/adapters-updated"


async def _default_request(payload: AgentRequestPayload):
    return payload.config


AGENT_REQUEST_SPEC = HookSpec(
    AGENT_REQUEST,
    "agent",
    HookMode.WATERFALL,
    payload_type=AgentRequestPayload,
    result_type=object,
    default=_default_request,
    scope=HookScope.AGENT,
)
ADAPTERS_UPDATED_SPEC = HookSpec(
    LLM_ADAPTERS_UPDATED,
    "llm",
    HookMode.EMIT,
    payload_type=AdaptersUpdatedPayload,
    failure_policy=HookFailurePolicy.OBSERVE,
    scope=HookScope.GLOBAL,
)


def spec_for(name: str) -> HookSpec:
    return {
        LLM_STREAM: LLM_STREAM_SPEC,
        AGENT_REQUEST: AGENT_REQUEST_SPEC,
        LLM_ADAPTERS_UPDATED: ADAPTERS_UPDATED_SPEC,
    }[name]


__all__ = [
    "ADAPTERS_UPDATED_SPEC",
    "AGENT_REQUEST",
    "AGENT_REQUEST_SPEC",
    "LLM_ADAPTERS_UPDATED",
    "LLM_STREAM",
    "LLM_STREAM_SPEC",
    "AgentRequestPayload",
    "LlmStreamPayload",
    "spec_for",
]
