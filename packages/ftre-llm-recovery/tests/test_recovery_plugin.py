import asyncio

import pytest
from cordis import Context
from ftre_agent import AgentRegistry
from ftre_agent.hooks import LLM_ERROR_SPEC, LLMErrorPayload
from ftre_llm_recovery.plugin import apply

from ftre.kernel.hooks import HookRuntime


@pytest.mark.asyncio
async def test_plugin_registers_policy_and_unmatched_calls_default():
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    apply(context, {"rules": {"timeout": {"action": "stop"}}})
    registry = AgentRegistry()
    registry.ensure("default")
    payload = LLMErrorPayload(
        session_id="s",
        turn_id="t",
        model="m",
        error_code="timeout",
        error_message="failed",
        iteration=1,
        attempt=1,
        max_attempts=2,
        cancellation=asyncio.Event(),
        agent_id="default",
    )
    result = await runtime.dispatch(
        LLM_ERROR_SPEC,
        payload,
        context=runtime.context_for_scope(registry.scope_carrier("default")),
    )
    assert result.action == "stop"
    context.dispose()
