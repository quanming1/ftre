from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cordis import Context
from ftre_compaction.plugin import apply

from ftre.kernel.hooks import HookRuntime
from ftre.plugins.builtin.command import CommandService
from ftre.services.agent.hooks import (
    AGENT_RUN_ERROR_SPEC,
    AgentSubject,
    RequestErrorPayload,
    RetryRequest,
)
from ftre.services.agent.registry import AgentRegistry
from ftre.services.messaging.bus import BusMessage, InboundMetadata


class _Sessions:
    async def get_session(self, _session_id):
        return {"channel_id": "ws"}


class _SessionEvents:
    async def emit(self, *_args, **_kwargs):
        return None


class _Config:
    """最小 ConfigService 替身：插件只需要读取 snapshot。"""

    def snapshot(self):
        return SimpleNamespace(value={})


def _config():
    return SimpleNamespace(
        llm=SimpleNamespace(context_window=1000, max_output=100, context_window_size=1000),
    )


@pytest.mark.asyncio
async def test_compaction_service_and_feature_hooks_register_separately():
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    context.provide("config", _Config())
    context.provide("llm", object())
    context.provide("sessions", _Sessions())
    context.provide("session_events", _SessionEvents())
    context.provide("inbox", object())
    context.provide("commands", type("Commands", (), {"register": lambda *_args, **_kwargs: lambda: True})())
    apply(context)

    assert context.get("compaction") is not None
    hooks = {item.hook for item in runtime.snapshot()}
    assert hooks == {"agent/after-run", "agent/run-error", "inbox/before-claim"}
    await context.dispose()


@pytest.mark.asyncio
async def test_overflow_hook_retries_only_after_generation_advances():
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    context.provide("config", _Config())
    context.provide("llm", object())
    context.provide("sessions", _Sessions())
    context.provide("session_events", _SessionEvents())
    context.provide("inbox", object())
    context.provide("commands", type("Commands", (), {"register": lambda *_args, **_kwargs: lambda: True})())
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
        AGENT_RUN_ERROR_SPEC,
        payload,
        context=runtime.context_for_scope(registry.scope_carrier("default")),
    )
    assert isinstance(result, RetryRequest)
    assert result.progress_token == "compaction:session-1:2"

    async def no_progress(*_args, **_kwargs):
        return False

    service.compact_if_needed = no_progress
    no_retry = await runtime.dispatch(
        AGENT_RUN_ERROR_SPEC,
        payload,
        context=runtime.context_for_scope(registry.scope_carrier("default")),
    )
    assert no_retry is None
    await context.dispose()


@pytest.mark.asyncio
async def test_compaction_service_effect_cancels_inflight_tasks_on_unload():
    context = Context()
    runtime = HookRuntime(context)
    context.provide("hook_runtime", runtime)
    context.provide("config", _Config())
    context.provide("llm", object())
    context.provide("sessions", _Sessions())
    context.provide("session_events", _SessionEvents())
    context.provide("inbox", object())
    context.provide("commands", type("Commands", (), {"register": lambda *_args, **_kwargs: lambda: True})())
    apply(context)
    service = context.get("compaction")
    task = asyncio.create_task(asyncio.sleep(60))
    service._compact_tasks["session-1"] = task

    await context.dispose()

    assert task.done()
    assert service._compact_tasks == {}


@pytest.mark.asyncio
async def test_compaction_commands_execute_directly_without_turn():
    context = Context()
    runtime = HookRuntime(context)
    commands = CommandService()
    context.provide("hook_runtime", runtime)
    context.provide("config", _Config())
    context.provide("llm", object())
    context.provide("sessions", _Sessions())
    context.provide("session_events", _SessionEvents())
    context.provide("inbox", object())
    context.provide("commands", commands)
    apply(context)
    service = context.get("compaction")
    service.compact_now = AsyncMock()
    service.compress_fast = AsyncMock()
    inbound = BusMessage(
        type="user_message",
        from_channel="ws",
        from_session="session-1",
        to_channel="agent",
        to_session="session-1",
        data={"session_id": "session-1", "content": "/compact keep auth"},
        metadata={"request_id": "command-1"},
    )

    definition = commands.parse({"inbound": inbound})
    result = await commands.dispatch_inbound(inbound, definition=definition)

    assert result is not None and result.kind == "success"
    service.compact_now.assert_awaited_once()
    fast_inbound = BusMessage(
        type="user_message",
        from_channel="ws",
        from_session="session-1",
        to_channel="agent",
        to_session="session-1",
        data={"session_id": "session-1", "content": "/compress-fast 2"},
        metadata=InboundMetadata(request_id="command-2"),
    )
    fast_definition = commands.parse({"inbound": fast_inbound})
    fast_result = await commands.dispatch_inbound(
        fast_inbound, definition=fast_definition
    )
    assert fast_result is not None and fast_result.kind == "success"
    service.compress_fast.assert_awaited_once()
    await context.dispose()
