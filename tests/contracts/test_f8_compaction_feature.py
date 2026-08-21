from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from cordis import Context

from ftre.features.compaction.plugin import apply
from ftre.platform.hooks import HookRuntime
from ftre.services.agent.hooks import (
    AGENT_REQUEST_ERROR_SPEC,
    AgentSubject,
    RequestErrorPayload,
    RetryRequest,
)
from ftre.services.agent.registry import AgentRegistry


class _Sessions:
    async def get_session(self, _session_id):
        return {"channel_id": "ws"}


def _config():
    return SimpleNamespace(
        llm=SimpleNamespace(context_window=1000, max_output=100, context_window_size=1000),
        context=SimpleNamespace(precompact_threshold=0.7, compact_threshold=0.8),
    )


@pytest.mark.asyncio
async def test_compaction_feature_registers_public_hooks_and_service():
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    context.provide("sessions", _Sessions())
    apply(context)

    assert context.get("compaction") is not None
    hooks = {item.hook for item in runtime.snapshot()}
    assert hooks == {"agent/pre-step", "agent/request-error"}
    await context.dispose()


@pytest.mark.asyncio
async def test_overflow_hook_retries_only_after_generation_advances():
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    context.provide("sessions", _Sessions())
    apply(context)
    service = context.get("compaction")
    service._progress_generation["session-1"] = 1

    async def compact_if_needed(*_args, **_kwargs):
        service._progress_generation["session-1"] = 2
        return True

    service.compact_if_needed = compact_if_needed
    registry = AgentRegistry()
    registry.ensure("default")
    payload = RequestErrorPayload(
        agent=AgentSubject("default", registry.scope_identity("default")),
        session_id="session-1",
        turn_id="turn-1",
        error_code="context_overflow",
        message="too large",
        attempt=0,
        cancellation=asyncio.Event(),
        channel_id="ws",
        config=_config(),
    )
    result = await runtime.dispatch(
        AGENT_REQUEST_ERROR_SPEC,
        payload,
        context=runtime.context_for_scope(registry.scope_carrier("default")),
    )
    assert isinstance(result, RetryRequest)
    assert result.progress_token == "compaction:session-1:2"
    await context.dispose()
