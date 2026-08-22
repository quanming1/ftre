from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from cordis import Context
from ftre_compaction.config import CompactionConfig
from ftre_compaction.hooks import register_hooks

from ftre.platform.hooks import HookRuntime
from ftre.services.agent.hooks import (
    AGENT_AFTER_TURN_SPEC,
    AGENT_PRE_STEP_SPEC,
    AfterTurnPayload,
    AgentSubject,
    PendingInput,
    PreStepPayload,
    RejectStep,
)
from ftre.services.agent.registry import AgentRegistry
from ftre.services.session.entity.state import QueueItem


class _Service:
    def __init__(
        self, *, fail: bool = False, stuck: bool = False, blocking: bool = False
    ) -> None:
        self.fail = fail
        self.stuck = stuck
        self.blocking = blocking
        self.calls: list[str] = []
        self._should_calls = 0
        self.cancel_calls: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.compaction_config = CompactionConfig()

    def config_for(self, _agent_config):
        """测试替身模拟真实 Service 的配置快照边界。"""
        return self.compaction_config

    async def should_compact(self, session_id, channel_id, config, **kwargs):
        self._should_calls += 1
        self.calls.append(f"should:{session_id}")
        return self.stuck or self._should_calls == 1

    async def compact(self, session_id, channel_id, **kwargs):
        self.calls.append(f"compact:{session_id}")
        self.started.set()
        if self.blocking:
            await self.release.wait()
        if self.fail:
            raise RuntimeError("summary failed")

    async def cancel_compact(self, session_id):
        self.cancel_calls.append(session_id)
        self.release.set()

    def progress_generation(self, _session_id):
        return 0


def _config():
    return SimpleNamespace(
        llm=SimpleNamespace(context_window=1000, max_output=100),
    )


def _context():
    context = Context()
    context.provide("hook_runtime", HookRuntime(context))
    context.provide("sessions", SimpleNamespace(get_session=lambda _sid: None))
    return context


@pytest.mark.asyncio
async def test_pre_step_compaction_marks_maintenance_and_returns_enter():
    context = _context()
    service = _Service()
    receipts = register_hooks(context, service)
    registry = AgentRegistry()
    record = registry.ensure("default")
    states: list[bool] = []

    async def mark(active, _reason):
        states.append(active)

    payload = PreStepPayload(
        agent=AgentSubject("default", record.identity),
        session_id="session-1",
        turn_id="turn-1",
        candidate=PendingInput.from_queue_item(
            QueueItem(request_id="request-1", sequence=1, content="hello")
        ),
        cancellation=asyncio.Event(),
        channel_id="ws",
        config=_config(),
        set_maintenance=mark,
    )
    result = await context.get("hook_runtime").dispatch(
        AGENT_PRE_STEP_SPEC,
        payload,
        context=context.get("hook_runtime").context_for_scope(
            registry.scope_carrier("default")
        ),
    )

    assert result.candidate.request_id == "request-1"
    assert states == [True, False]
    assert service.calls == [
        "should:session-1",
        "compact:session-1",
        "should:session-1",
    ]
    for receipt in receipts:
        receipt.dispose()
    context.dispose()


@pytest.mark.asyncio
async def test_pre_step_compaction_failure_keeps_pending():
    context = _context()
    service = _Service(fail=True)
    receipts = register_hooks(context, service)
    registry = AgentRegistry()
    record = registry.ensure("default")
    payload = PreStepPayload(
        agent=AgentSubject("default", record.identity),
        session_id="session-1",
        turn_id="turn-1",
        candidate=PendingInput.from_queue_item(
            QueueItem(request_id="request-1", sequence=1, content="hello")
        ),
        cancellation=asyncio.Event(),
        channel_id="ws",
        config=_config(),
    )
    result = await context.get("hook_runtime").dispatch(
        AGENT_PRE_STEP_SPEC,
        payload,
        context=context.get("hook_runtime").context_for_scope(
            registry.scope_carrier("default")
        ),
    )

    assert isinstance(result, RejectStep)
    assert result.disposition == "keep"
    for receipt in receipts:
        receipt.dispose()
    context.dispose()


@pytest.mark.asyncio
async def test_pre_step_rejects_when_compaction_does_not_reach_safe_watermark():
    context = _context()
    service = _Service(stuck=True)
    receipts = register_hooks(context, service)
    registry = AgentRegistry()
    record = registry.ensure("default")
    payload = PreStepPayload(
        agent=AgentSubject("default", record.identity),
        session_id="session-1",
        turn_id="turn-1",
        candidate=PendingInput.from_queue_item(
            QueueItem(request_id="request-1", sequence=1, content="hello")
        ),
        cancellation=asyncio.Event(),
        channel_id="ws",
        config=_config(),
    )
    result = await context.get("hook_runtime").dispatch(
        AGENT_PRE_STEP_SPEC,
        payload,
        context=context.get("hook_runtime").context_for_scope(
            registry.scope_carrier("default")
        ),
    )

    assert isinstance(result, RejectStep)
    assert result.disposition == "keep"
    assert "安全水位" in result.reason
    for receipt in receipts:
        receipt.dispose()
    context.dispose()


@pytest.mark.asyncio
async def test_after_turn_uses_precompact_threshold_and_serial_hook_boundary():
    context = _context()
    service = _Service()
    receipts = register_hooks(context, service)
    registry = AgentRegistry()
    record = registry.ensure("default")
    payload = AfterTurnPayload(
        agent=AgentSubject("default", record.identity),
        session_id="session-1",
        turn_id="turn-1",
        request_id="request-1",
        status="completed",
        cancellation=asyncio.Event(),
        channel_id="ws",
        config=_config(),
    )
    result = await context.get("hook_runtime").dispatch(
        AGENT_AFTER_TURN_SPEC,
        payload,
        context=context.get("hook_runtime").context_for_scope(
            registry.scope_carrier("default")
        ),
    )

    assert result is None
    assert service.calls == [
        "should:session-1",
        "compact:session-1",
        "should:session-1",
    ]
    for receipt in receipts:
        receipt.dispose()
    context.dispose()


@pytest.mark.asyncio
async def test_cancelled_compaction_hook_cancels_service_task():
    context = _context()
    service = _Service(blocking=True)
    receipts = register_hooks(context, service)
    registry = AgentRegistry()
    record = registry.ensure("default")
    payload = PreStepPayload(
        agent=AgentSubject("default", record.identity),
        session_id="session-1",
        turn_id="turn-1",
        candidate=PendingInput.from_queue_item(
            QueueItem(request_id="request-1", sequence=1, content="hello")
        ),
        cancellation=asyncio.Event(),
        channel_id="ws",
        config=_config(),
    )
    task = asyncio.create_task(
        context.get("hook_runtime").dispatch(
            AGENT_PRE_STEP_SPEC,
            payload,
            context=context.get("hook_runtime").context_for_scope(
                registry.scope_carrier("default")
            ),
        )
    )
    await service.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert service.cancel_calls == ["session-1"]
    for receipt in receipts:
        receipt.dispose()
    context.dispose()
