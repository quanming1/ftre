"""F6.10 lifecycle, scope, cancellation and mailbox fault contracts."""

from __future__ import annotations

import asyncio

import pytest
from cordis import Context, FiberState

from ftre.platform.hooks import (
    HookMode,
    HookRuntime,
    HookScope,
    HookSpec,
)
from ftre.services.agent.config import AgentConfig
from ftre.services.agent.hooks import (
    AGENT_PRE_STEP_SPEC,
    AGENT_REQUEST_ERROR_SPEC,
    RetryRequest,
)
from ftre.services.agent.registry import AgentRegistry
from ftre.services.agent_loop.runtime.loop.context_gate import ContextDecision
from ftre.services.agent_loop.runtime.loop.engine import AgentLoop
from ftre.services.agent_loop.runtime.loop.turn_executor import (
    Turn,
    TurnExecutor,
    TurnOutcome,
)
from ftre.services.agent_loop.runtime.mailbox.lane import (
    BlockedOperation,
    SessionLane,
)
from ftre.services.messaging.bus import BusMessage
from ftre.services.session.entity.state import MailboxState, QueueItem
from ftre.services.session.hooks import (
    SESSION_CREATED_SPEC,
    SESSION_DISPOSED_SPEC,
    SessionLifecyclePayload,
)
from ftre.services.session.service import SessionService


def _global_spec() -> HookSpec:
    async def default(payload):
        return payload

    return HookSpec(
        "test/lifecycle",
        "test",
        HookMode.PARALLEL,
        payload_type=dict,
        default=default,
    )


async def _dispose_context(context: Context) -> None:
    cleanup = context.dispose()
    if cleanup is not None:
        await cleanup


@pytest.mark.asyncio
async def test_fiber_restart_removes_old_listener_and_reloads_once() -> None:
    root = Context()
    runtime = HookRuntime(root)
    spec = _global_spec()
    seen: list[str] = []

    def plugin(ctx, _config=None):
        runtime.register(
            spec,
            lambda _payload: seen.append("listener"),
            owner="reload-plugin",
            context=ctx,
        )

    fiber = root.plugin(plugin)
    await fiber
    assert fiber.state is FiberState.ACTIVE
    await runtime.dispatch(spec, {})
    assert seen == ["listener"]

    await fiber.restart()
    assert fiber.state is FiberState.ACTIVE
    active = [entry for entry in runtime.snapshot(spec.name) if not entry.disposed]
    assert len(active) == 1
    await runtime.dispatch(spec, {})
    assert seen == ["listener", "listener"]

    cleanup = fiber.dispose()
    if cleanup is not None:
        await cleanup
    assert not [entry for entry in runtime.snapshot(spec.name) if not entry.disposed]
    await _dispose_context(root)


@pytest.mark.asyncio
async def test_fiber_dispose_waits_for_inflight_hook_and_marks_quiescence() -> None:
    root = Context()
    runtime = HookRuntime(root)
    spec = _global_spec()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking(_payload):
        started.set()
        await release.wait()

    def plugin(ctx, _config=None):
        runtime.register(spec, blocking, owner="inflight", context=ctx)

    fiber = root.plugin(plugin)
    await fiber
    dispatch_task = asyncio.create_task(runtime.dispatch(spec, {}))
    await started.wait()

    cleanup = fiber.dispose()
    assert cleanup is not None

    async def wait_cleanup():
        await cleanup

    cleanup_task = asyncio.create_task(wait_cleanup())
    await asyncio.sleep(0)
    assert cleanup_task.done() is False
    assert runtime.snapshot(spec.name)[0].active_calls == 1

    release.set()
    await dispatch_task
    await cleanup_task
    snapshot = runtime.snapshot(spec.name)[0]
    assert snapshot.disposed is True
    assert snapshot.active_calls == 0
    await _dispose_context(root)


@pytest.mark.asyncio
async def test_agent_scope_rebuilt_id_does_not_inherit_old_listener() -> None:
    root = Context()
    runtime = HookRuntime(root)
    registry = AgentRegistry()
    spec = HookSpec(
        "test/scoped",
        "test",
        HookMode.PARALLEL,
        payload_type=dict,
        scope=HookScope.AGENT,
    )
    old = registry.ensure("worker")
    old_context = runtime.context_for_scope(registry.scope_carrier("worker"))
    seen: list[str] = []
    receipt = runtime.register(
        spec,
        lambda _payload: seen.append("old"),
        owner="old-agent",
        context=old_context,
        scope="agent:worker",
    )

    await runtime.dispatch(spec, {}, context=old_context)
    registry.dispose("worker")
    new = registry.ensure("worker")
    assert new.identity is not old.identity
    new_context = runtime.context_for_scope(registry.scope_carrier("worker"))
    await runtime.dispatch(spec, {}, context=new_context)
    assert seen == ["old"]
    receipt.dispose()
    await _dispose_context(root)


class _Mailbox:
    def __init__(self, *items: QueueItem) -> None:
        self.items = list(items)
        self.calls: list[str] = []
        self.sequence = len(self.items) + 1

    async def admit(self, inbound):
        self.calls.append("admit")
        item = QueueItem(
            request_id=f"request-{self.sequence}",
            sequence=self.sequence,
            content=inbound.data.get("content", ""),
            agent_id=inbound.metadata.agent_id or "default",
        )
        self.sequence += 1
        self.items.append(item)
        return type("Admission", (), {
            "session_id": "session-1",
            "request_id": item.request_id,
            "queue_position": len(self.items),
            "created": True,
        })()

    async def peek(self, _session_id):
        self.calls.append("peek")
        return self.items[0] if self.items else None

    async def take(self, _session_id, request_id):
        self.calls.append("take")
        if self.items and self.items[0].request_id == request_id:
            return self.items.pop(0)
        return None

    async def cancel_pending(self, _session_id, request_id):
        self.calls.append("cancel")
        if self.items and self.items[0].request_id == request_id:
            return self.items.pop(0)
        return None

    async def channel_id(self, _session_id):
        return "test"

    async def snapshot(self, _session_id):
        return MailboxState(pending=list(self.items))

    async def advance_revision(self, _session_id):
        self.calls.append("revision")
        return 1


class _Completion:
    def __init__(self) -> None:
        self.results: dict[str, TurnOutcome] = {}

    async def complete(self, _session_id, request_id, result):
        self.results[request_id] = result

    async def close_session(self, _session_id):
        return None


class _Executor:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def resolve_inbound_config(self, *_args, **_kwargs):
        return AgentConfig(), None

    async def execute(self, inbound, **_kwargs):
        self.seen.append(inbound.data["content"])
        return TurnOutcome(turn_id="turn", status="completed")


def _inbound(content: str, request_id: str = "") -> BusMessage:
    return BusMessage(
        type="user_message",
        from_channel="test",
        from_session="session-1",
        to_channel="agent",
        to_session="session-1",
        data={"session_id": "session-1", "content": content},
        metadata={"request_id": request_id},
    )


class _PassGate:
    async def before_claim(self, *_args):
        return ContextDecision("pass")

    async def after_turn(self, *_args):
        return ContextDecision("pass")


class _FailingCompactionGate:
    async def before_claim(self, *_args):
        return ContextDecision("compact", "pressure")

    async def compact(self, *_args):
        return ContextDecision("block", "compaction failed")


@pytest.mark.asyncio
async def test_compaction_failure_keeps_pending_until_explicit_cancel() -> None:
    item = QueueItem(request_id="pending-1", sequence=1, content="A")
    mailbox = _Mailbox(item)
    completion = _Completion()
    lane = SessionLane(
        "session-1",
        mailbox=mailbox,
        context_gate=_FailingCompactionGate(),
        executor=_Executor(),
        completion=completion,
        publish_snapshot=lambda _sid: asyncio.sleep(0),
    )

    await lane._drain()
    assert mailbox.items == [item]
    assert isinstance(lane.operation, BlockedOperation)
    cancelled = await lane.cancel_pending("pending-1")
    assert cancelled is item
    assert completion.results["pending-1"].status == "cancelled"
    if lane._worker is not None:
        await lane._worker
    await lane.close()


@pytest.mark.asyncio
async def test_pre_step_failure_retries_pending_once_without_duplicate_execution() -> None:
    root = Context()
    hooks = HookRuntime(root)
    calls = 0

    async def flaky(_payload, next_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("policy temporarily unavailable")
        return await next_()

    hooks.register(
        AGENT_PRE_STEP_SPEC,
        flaky,
        owner="flaky-policy",
        global_listener=True,
    )
    mailbox = _Mailbox(QueueItem(request_id="pending-1", sequence=1, content="A"))
    executor = _Executor()
    lane = SessionLane(
        "session-1",
        mailbox=mailbox,
        context_gate=_PassGate(),
        executor=executor,
        completion=_Completion(),
        publish_snapshot=lambda _sid: asyncio.sleep(0),
        hooks=hooks,
        agent_registry=AgentRegistry(),
    )

    await lane._drain()
    assert [item.request_id for item in mailbox.items] == ["pending-1"]
    assert isinstance(lane.operation, BlockedOperation)

    await lane.submit(_inbound("B", "client-B"))
    worker = lane._worker
    if worker is not None:
        await worker
    assert executor.seen == ["A", "B"]
    assert calls == 3
    await lane.close()
    await _dispose_context(root)


@pytest.mark.asyncio
async def test_request_retry_is_rejected_after_turn_cancellation() -> None:
    root = Context()
    loop = object.__new__(AgentLoop)
    loop.hooks = HookRuntime(root)
    loop.agent_registry = AgentRegistry()
    called = 0

    async def retry(_payload, _next_):
        nonlocal called
        called += 1
        return RetryRequest("recovered", "generation-1")

    loop.hooks.register(
        AGENT_REQUEST_ERROR_SPEC,
        retry,
        owner="recovery",
        global_listener=True,
    )
    turn = Turn("turn-1", _inbound("hello"), "session-1")
    turn.cancellation.set()
    executor = TurnExecutor(loop)
    executor._emit_step = _noop_emit

    assert await executor._request_error_recovery(
        turn, error_code="context_overflow", message="too large"
    ) is False
    assert called == 1
    assert turn.retry_count == 0
    await _dispose_context(root)


@pytest.mark.asyncio
async def test_session_created_disposed_hooks_follow_persistence_commit(tmp_path) -> None:
    root = Context()
    runtime = HookRuntime(root)
    session = SessionService(sessions_dir=str(tmp_path / "sessions"))
    await session.init()
    seen: list[tuple[str, str]] = []

    def observe_created(_payload: SessionLifecyclePayload):
        seen.append(("created", _payload.session_id))

    def observe_disposed(_payload: SessionLifecyclePayload):
        seen.append(("disposed", _payload.session_id))

    runtime.register(
        SESSION_CREATED_SPEC,
        observe_created,
        owner="test-created",
        global_listener=True,
    )
    runtime.register(
        SESSION_DISPOSED_SPEC,
        observe_disposed,
        owner="test-disposed",
        global_listener=True,
    )

    async def dispatch(kind: str, session_id: str, channel_id: str):
        del channel_id
        spec = SESSION_CREATED_SPEC if kind == "created" else SESSION_DISPOSED_SPEC
        await runtime.dispatch(spec, SessionLifecyclePayload(session_id))

    unbind = session.bind_lifecycle_dispatcher(dispatch)
    session_id = await session.create_session("test")
    await session.delete_session(session_id)
    assert seen == [("created", session_id), ("disposed", session_id)]
    assert unbind() is True
    await _dispose_context(root)


async def _noop_emit(*_args, **_kwargs):
    return None
