import asyncio
from types import SimpleNamespace

import pytest
from cordis import Context
from ftre_llm_fallback.plugin import apply

from ftre.kernel.hooks import HookRuntime
from ftre.services.llm.hooks import LLM_STREAM_SPEC, LLMStreamPayload


@pytest.mark.asyncio
async def test_plugin_registers_only_when_configured():
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    context.provide("config", SimpleNamespace(resolve_llm=lambda *_: None))
    apply(context, {"provider": "p", "model": "m", "errors": ["timeout"]})

    assert [item.owner for item in runtime.snapshot(LLM_STREAM_SPEC.name)] == [
        "ftre-llm-fallback"
    ]
    context.dispose()


def test_payload_fixture_has_last_attempt_coordinates():
    payload = LLMStreamPayload(
        agent_id="a",
        session_id="s",
        turn_id="t",
        model="primary",
        messages=(),
        tools=(),
        cancellation=asyncio.Event(),
        invoke=lambda: (),
        attempt=3,
        max_attempts=3,
    )
    assert payload.attempt == payload.max_attempts
