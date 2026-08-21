from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from cordis import Context

from ftre.platform.hooks import HookRuntime
from ftre.services.agent.config import AgentConfig
from ftre.services.agent.hooks import (
    AGENT_INBOX_CLAIMED_SPEC,
    AGENT_INBOX_INSERTED_SPEC,
    AGENT_PRE_STEP_SPEC,
    AGENT_REQUEST_ERROR_SPEC,
    AGENT_REQUEST_SPEC,
    AGENT_TURN_STOPPED_SPEC,
    EnterStep,
    RejectStep,
    RetryRequest,
)
from ftre.services.agent.registry import AgentRegistry
from ftre.services.agent_loop.runtime.loop.engine import AgentLoop
from ftre.services.agent_loop.runtime.mailbox.lane import (
    RequestAdmission,
    SessionLane,
    TurnOutcome,
)
from ftre.services.messaging.bus import BusMessage
from ftre.services.session.entity.state import MailboxState, QueueItem


class FakeMailbox:
    def __init__(self, item: QueueItem | None) -> None:
        self.items = [item] if item is not None else []
        self.calls: list[str] = []

    async def admit(self, inbound):
        self.calls.append("admit")
        item = QueueItem(
            request_id="request-inserted",
            sequence=1,
            content=str(inbound.data.get("content") or ""),
            agent_id=inbound.metadata.agent_id or "default",
        )
        self.items.append(item)
        return RequestAdmission("session-1", item.request_id, True, len(self.items))

    async def peek(self, _session_id):
        self.calls.append("peek")
        return self.items[0] if self.items else None

    async def take(self, _session_id, request_id):
        self.calls.append("take")
        if self.items and self.items[0].request_id == request_id:
            return self.items.pop(0)
        return None

    async def cancel_pending(self, _session_id, request_id):
        self.calls.append("discard")
        if self.items and self.items[0].request_id == request_id:
            return self.items.pop(0)
        return None

    async def channel_id(self, _session_id):
        return "test"

    async def advance_revision(self, _session_id):
        self.calls.append("revision")
        return 1

    async def snapshot(self, _session_id):
        return MailboxState(pending=list(self.items))


class PassGate:
    async def before_claim(self, *_args):
        return type("Decision", (), {"action": "pass", "reason": ""})()

    async def after_turn(self, *_args):
        return type("Decision", (), {"action": "pass", "reason": ""})()


class FakeCompletion:
    def __init__(self) -> None:
        self.results: list[tuple[str, str]] = []

    async def complete(self, *_args):
        if len(_args) >= 3:
            self.results.append((_args[1], _args[2].status))

    async def close_session(self, *_args):
        return None


class FakeExecutor:
    async def resolve_inbound_config(self, *_args, **_kwargs):
        return AgentConfig(), None

    async def execute(self, *_args, **_kwargs):
        return TurnOutcome(turn_id="turn", status="completed")


async def _publish(_session_id):
    return None


def _item() -> QueueItem:
    return QueueItem(request_id="request-1", sequence=1, content="hello")


def _inbound() -> BusMessage:
    return BusMessage(
        type="user_message",
        from_channel="test",
        from_session="session-1",
        to_channel="agent",
        to_session="session-1",
        data={"session_id": "session-1", "content": "hello"},
    )


@pytest.mark.asyncio
async def test_pre_step_runs_before_claim_and_keep_reject_preserves_pending():
    context = Context()
    hooks = HookRuntime(context)
    mailbox = FakeMailbox(_item())
    registry = AgentRegistry()
    seen: list[str] = []

    async def reject(payload, _next_):
        seen.append(payload.candidate.request_id)
        return RejectStep("keep", "policy")

    hooks.register(
        AGENT_PRE_STEP_SPEC,
        reject,
        owner="policy",
        global_listener=True,
    )
    lane = SessionLane(
        "session-1",
        mailbox=mailbox,
        context_gate=PassGate(),
        executor=FakeExecutor(),
        completion=FakeCompletion(),
        publish_snapshot=_publish,
        hooks=hooks,
        agent_registry=registry,
    )
    await lane._drain()

    assert seen == ["request-1"]
    assert mailbox.items[0].request_id == "request-1"
    assert "take" not in mailbox.calls
    assert mailbox.calls.index("peek") < mailbox.calls.index("revision")


@pytest.mark.asyncio
async def test_pre_step_enter_claims_after_hook_and_executes_once():
    context = Context()
    hooks = HookRuntime(context)
    mailbox = FakeMailbox(_item())
    executed: list[str] = []

    async def enter(payload, next_):
        executed.append("hook")
        result = await next_()
        assert isinstance(result, EnterStep)
        return result

    hooks.register(AGENT_PRE_STEP_SPEC, enter, owner="allow", global_listener=True)
    executor = FakeExecutor()
    lane = SessionLane(
        "session-1",
        mailbox=mailbox,
        context_gate=PassGate(),
        executor=executor,
        completion=FakeCompletion(),
        publish_snapshot=_publish,
        hooks=hooks,
        agent_registry=AgentRegistry(),
    )
    await lane._drain()

    assert executed == ["hook"]
    assert mailbox.items == []
    assert mailbox.calls.index("take") > mailbox.calls.index("peek")


@pytest.mark.asyncio
async def test_pre_step_discard_claims_nothing_and_completes_cancelled():
    context = Context()
    hooks = HookRuntime(context)
    mailbox = FakeMailbox(_item())
    completion = FakeCompletion()

    async def discard(payload, _next_):
        return RejectStep("discard", "policy")

    hooks.register(AGENT_PRE_STEP_SPEC, discard, owner="policy", global_listener=True)
    lane = SessionLane(
        "session-1",
        mailbox=mailbox,
        context_gate=PassGate(),
        executor=FakeExecutor(),
        completion=completion,
        publish_snapshot=_publish,
        hooks=hooks,
        agent_registry=AgentRegistry(),
    )
    await lane._drain()

    assert mailbox.items == []
    assert "take" not in mailbox.calls
    assert "discard" in mailbox.calls
    assert completion.results == [("request-1", "cancelled")]


@pytest.mark.asyncio
async def test_request_error_retry_is_bounded_by_progress_token():
    context = Context()
    loop = object.__new__(AgentLoop)
    loop.hooks = HookRuntime(context)
    loop.agent_registry = AgentRegistry()
    loop._agent_created_emitted = set()
    loop.session_manager = AsyncMock()
    calls: list[int] = []

    async def recover(payload, _next_):
        calls.append(payload.attempt)
        return RetryRequest("compact", "generation-1", max_attempts=2)

    loop.hooks.register(
        AGENT_REQUEST_ERROR_SPEC,
        recover,
        owner="recovery",
        global_listener=True,
    )
    from ftre.services.agent_loop.runtime.loop.turn_executor import Turn, TurnExecutor

    turn = Turn("turn-1", _inbound(), "session-1")
    executor = TurnExecutor(
        loop,
        sessions=loop.session_manager,
        hooks=loop.hooks,
        agent_registry=loop.agent_registry,
    )

    async def no_emit(*_args, **_kwargs):
        return None

    executor._emit_step = no_emit
    assert await executor._request_error_recovery(
        turn, error_code="overflow", message="too long"
    ) is True
    assert await executor._request_error_recovery(
        turn, error_code="overflow", message="too long"
    ) is False
    assert calls == [0, 1]


@pytest.mark.asyncio
async def test_request_and_turn_stopped_hooks_use_typed_boundaries():
    context = Context()
    loop = object.__new__(AgentLoop)
    loop.hooks = HookRuntime(context)
    loop.agent_registry = AgentRegistry()
    loop._agent_created_emitted = set()
    loop.session_manager = AsyncMock()
    observed: list[str] = []
    signals: list[asyncio.Event] = []

    async def route(payload, next_):
        signals.append(payload.cancellation)
        config = await next_()
        config.workspace = "hooked"
        return config

    async def stopping(payload):
        observed.append(payload.request_id)

    loop.hooks.register(AGENT_REQUEST_SPEC, route, owner="router", global_listener=True)
    loop.hooks.register(
        AGENT_TURN_STOPPED_SPEC,
        stopping,
        owner="observer",
        global_listener=True,
    )
    from ftre.services.agent_loop.runtime.loop.turn_executor import Turn, TurnExecutor

    turn = Turn("turn-1", _inbound(), "session-1", config=AgentConfig())
    executor = TurnExecutor(
        loop,
        sessions=loop.session_manager,
        hooks=loop.hooks,
        agent_registry=loop.agent_registry,
    )
    result = await executor._request_config(turn, turn.config)
    await executor._notify_turn_stopped(turn)
    await asyncio.sleep(0)
    assert result.workspace == "hooked"
    assert observed == [""]
    assert signals == [turn.cancellation]


@pytest.mark.asyncio
async def test_inbox_observations_follow_real_mutations():
    context = Context()
    hooks = HookRuntime(context)
    mailbox = FakeMailbox(_item())
    observed: list[tuple[str, str, str]] = []

    def observe(payload):
        observed.append((payload.item.request_id, payload.session_id, payload.turn_id))

    hooks.register(AGENT_INBOX_INSERTED_SPEC, observe, owner="audit", global_listener=True)
    hooks.register(AGENT_INBOX_CLAIMED_SPEC, observe, owner="audit", global_listener=True)

    lane = SessionLane(
        "session-1",
        mailbox=mailbox,
        context_gate=PassGate(),
        executor=FakeExecutor(),
        completion=FakeCompletion(),
        publish_snapshot=_publish,
        hooks=hooks,
        agent_registry=AgentRegistry(),
    )
    accepted = await lane.submit(_inbound())
    assert accepted.created is True
    if lane._worker is not None:
        await lane._worker
    assert observed[0] == ("request-inserted", "session-1", "")
    claimed = [event for event in observed if event[0] == "request-inserted"]
    assert claimed[1][0:2] == ("request-inserted", "session-1")
    assert claimed[1][2].startswith("turn_")


@pytest.mark.asyncio
async def test_pre_step_cancellation_signal_is_set_before_lane_close():
    context = Context()
    hooks = HookRuntime(context)
    started = asyncio.Event()
    seen: list[asyncio.Event] = []

    async def wait_for_close(payload, _next_):
        seen.append(payload.cancellation)
        started.set()
        await asyncio.sleep(10)
        return await _next_()

    hooks.register(
        AGENT_PRE_STEP_SPEC,
        wait_for_close,
        owner="blocking-policy",
        global_listener=True,
    )
    lane = SessionLane(
        "session-1",
        mailbox=FakeMailbox(_item()),
        context_gate=PassGate(),
        executor=FakeExecutor(),
        completion=FakeCompletion(),
        publish_snapshot=_publish,
        hooks=hooks,
        agent_registry=AgentRegistry(),
    )
    worker = asyncio.create_task(lane._drain())
    lane._worker = worker
    await started.wait()
    await lane.close()
    await asyncio.gather(worker, return_exceptions=True)
    assert seen and seen[0].is_set()
