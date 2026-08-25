import asyncio

import pytest
from cordis import Context
from ftre_agent_core.agent import ReActAgent
from ftre_agent_core.event import EventType
from ftre_agent_core.llm import LLMError
from ftre_llm_recovery.plugin import apply

from ftre.kernel.hooks import HookRuntime
from ftre.services.agent.registry import AgentRegistry


@pytest.mark.asyncio
async def test_plugin_decision_reaches_real_core_retry_boundary():
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    apply(context, {"rules": {"rate_limit": {"action": "stop"}}})

    registry = AgentRegistry()
    registry.ensure("default")
    scope = runtime.context_for_scope(registry.scope_carrier("default"))
    agent = ReActAgent(
        model="fake",
        api_key="fake",
        max_retries=3,
        retry_delay=0,
        hooks=runtime,
        hook_context=scope,
    )

    calls = 0

    async def fail(messages, tools=None):
        nonlocal calls
        del messages, tools
        calls += 1
        raise LLMError(message="rate limited", code="rate_limit")
        yield

    agent.runner._llm.stream = fail
    events = [
        event
        async for event in agent.run(
            "hello",
            runtime_context={
                "session_id": "session-1",
                "agent_id": "default",
                "cancellation": asyncio.Event(),
            },
        )
    ]

    assert calls == 1
    assert agent.run_state.error_code == "rate_limit"
    assert not [event for event in events if event.type == EventType.RETRY]
    context.dispose()
