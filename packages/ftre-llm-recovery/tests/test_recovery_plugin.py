import asyncio

import pytest
from cordis import Context
from ftre_llm_recovery.plugin import apply

from ftre.kernel.hooks import HookRuntime
from ftre.services.agent.registry import AgentRegistry
from ftre.services.llm.hooks import LLM_ERROR_SPEC, LLMErrorPayload


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
        iteration=1,
        model="m",
        error_code="timeout",
        error_message="failed",
        attempt=1,
        max_attempts=2,
        cancellation=asyncio.Event(),
    )
    result = await runtime.dispatch(
        LLM_ERROR_SPEC,
        payload,
        context=runtime.context_for_scope(registry.scope_carrier("default")),
    )
    assert result.action == "stop"
    context.dispose()
