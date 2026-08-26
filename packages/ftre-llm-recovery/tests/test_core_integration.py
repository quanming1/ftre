from __future__ import annotations

import asyncio

import pytest
from cordis import Context
from ftre_agent import AgentRegistry
from ftre_agent_core.hooks import LLM_ERROR_SPEC, LLMErrorDecision, LLMErrorPayload
from ftre_llm_recovery.plugin import apply

from ftre.kernel.hooks import HookRuntime


@pytest.mark.asyncio
async def test_plugin_decision_is_exposed_at_request_error_boundary():
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    apply(context, {"rules": {"rate_limit": {"action": "retry"}}})

    registry = AgentRegistry()
    registry.ensure("default")
    payload = LLMErrorPayload(
        session_id="session-1",
        turn_id="turn-1",
        model="model",
        error_code="rate_limit",
        error_message="rate limited",
        iteration=1,
        attempt=1,
        max_attempts=3,
        cancellation=asyncio.Event(),
        agent_id="default",
    )
    result = await runtime.dispatch(
        LLM_ERROR_SPEC,
        payload,
        context=runtime.context_for_scope(registry.scope_carrier("default")),
    )

    assert isinstance(result, LLMErrorDecision)
    assert result.action == "retry"
    assert result.reason == "configured recovery for rate_limit"
    context.dispose()
