from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from cordis import Context
from ftre_compaction.config import CompactionConfig
from ftre_compaction.hooks import register_hooks
from ftre_inbox.hooks import (
    INBOX_BEFORE_CLAIM_SPEC,
    BeforeClaimPayload,
    EnterClaim,
    RejectClaim,
)
from ftre_inbox.models import QueueItem

from ftre.platform.hooks import HookRuntime
from ftre.services.agent.hooks import (
    AGENT_AFTER_TURN_SPEC,
    AfterTurnPayload,
    AgentSubject,
)
from ftre.services.agent.registry import AgentRegistry


class _Service:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []
        self._should_calls = 0
        self.compaction_config = CompactionConfig()

    def config_for(self, _agent_config):
        return self.compaction_config

    async def should_compact(self, session_id, channel_id, config, **kwargs):
        self._should_calls += 1
        self.calls.append(f"should:{session_id}")
        return self._should_calls == 1

    async def compact(self, session_id, channel_id, **kwargs):
        self.calls.append(f"compact:{session_id}")
        if self.fail:
            raise RuntimeError("summary failed")

    async def cancel_compact(self, _session_id):
        return None

    def progress_generation(self, _session_id):
        return 0


def _config():
    return SimpleNamespace(llm=SimpleNamespace(context_window=1000, max_output=100))


def _context(*, inbox=True):
    context = Context()
    context.provide("hook_runtime", HookRuntime(context))
    context.provide("sessions", SimpleNamespace(get_session=lambda _sid: None))
    if inbox:
        context.provide("inbox", object())
    return context


@pytest.mark.asyncio
async def test_after_turn_compaction_is_owned_by_package_hook():
    context = _context(inbox=False)
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
    assert service.calls == ["should:session-1", "compact:session-1", "should:session-1"]
    for receipt in receipts:
        receipt.dispose()
    context.dispose()


@pytest.mark.asyncio
async def test_inbox_before_claim_returns_enter_after_compaction():
    context = _context()
    service = _Service()
    receipts = register_hooks(context, service)
    item = QueueItem("request-1", 1, "session-1", "ws", "hello")
    payload = BeforeClaimPayload(
        session_id="session-1",
        candidate=item,
        candidates=(item,),
        target="next-turn",
        channel_id="ws",
        cancellation=asyncio.Event(),
    )
    result = await context.get("hook_runtime").dispatch(INBOX_BEFORE_CLAIM_SPEC, payload)
    assert isinstance(result, EnterClaim)
    assert result.request_id == "request-1"
    for receipt in receipts:
        receipt.dispose()
    context.dispose()


@pytest.mark.asyncio
async def test_inbox_before_claim_failure_keeps_pending():
    context = _context()
    service = _Service(fail=True)
    receipts = register_hooks(context, service)
    item = QueueItem("request-1", 1, "session-1", "ws", "hello")
    payload = BeforeClaimPayload(
        session_id="session-1",
        candidate=item,
        candidates=(item,),
        target="next-turn",
        channel_id="ws",
        cancellation=asyncio.Event(),
    )
    result = await context.get("hook_runtime").dispatch(INBOX_BEFORE_CLAIM_SPEC, payload)
    assert isinstance(result, RejectClaim)
    assert result.disposition == "keep"
    for receipt in receipts:
        receipt.dispose()
    context.dispose()
