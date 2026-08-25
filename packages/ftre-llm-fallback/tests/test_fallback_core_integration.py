from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from cordis import Context
from ftre_agent_core.agent import ReActAgent
from ftre_agent_core.llm import (
    BlockEnd,
    BlockStart,
    FinishChunk,
    FinishReason,
    LlmFailure,
    TextDeltaChunk,
)
from ftre_llm_fallback.plugin import apply

from ftre.kernel.hooks import HookRuntime
from ftre.services.agent.registry import AgentRegistry


@pytest.mark.asyncio
async def test_last_attempt_fallback_reaches_real_core(monkeypatch):
    class Backup:
        async def stream(self, messages, tools=None):
            del messages, tools
            yield BlockStart(index=0, block_type="text")
            yield TextDeltaChunk(index=0, text="backup answer")
            yield BlockEnd(index=0, block={"type": "text", "text": "backup answer"})
            yield FinishChunk(reason=FinishReason(kind="stop"))

        def cancel(self):
            return None

    monkeypatch.setattr("ftre_llm_fallback.stream._create_backup_adapter", lambda _: Backup())
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    context.provide("config", SimpleNamespace(resolve_llm=lambda *_: {"model": "backup"}))
    apply(context, {"provider": "p", "model": "m", "errors": ["timeout"]})

    registry = AgentRegistry()
    registry.ensure("default")
    agent = ReActAgent(
        model="primary",
        api_key="fake",
        max_retries=0,
        hooks=runtime,
        hook_context=runtime.context_for_scope(registry.scope_carrier("default")),
    )

    async def primary(messages, tools=None):
        del messages, tools
        yield FinishChunk(
            reason=FinishReason(
                kind="error",
                failure=LlmFailure(code="timeout", message="primary failed"),
            )
        )

    agent.runner._llm.stream = primary
    events = [
        event
        async for event in agent.run(
            "hello",
            runtime_context={"session_id": "s", "cancellation": asyncio.Event()},
        )
    ]

    assert agent.run_state.error_code is None
    assert any(getattr(event, "delta", "") == "backup answer" for event in events)
    context.dispose()
