import asyncio

import pytest
from cordis import Context
from ftre_inbox.hooks import INBOX_BEFORE_CLAIM_SPEC, RejectClaim
from ftre_inbox.repository import InboxRepository
from ftre_inbox.service import InboxService

from ftre.platform.hooks import HookRuntime
from ftre.services.agent.contracts import InboundMessage


class FakeAgent:
    def __init__(self):
        self.running = set()
        self.received = []
        self.done = asyncio.Event()

    def is_busy(self, session_id):
        return session_id in self.running

    async def run(self, message):
        self.running.add(message.session_id)
        self.received.append(message)
        await asyncio.sleep(0)
        self.running.remove(message.session_id)
        self.done.set()
        return {"status": "completed"}

    async def cancel(self, _session_id):
        return False


class CancellableAgent(FakeAgent):
    async def cancel(self, _session_id):
        self.running.clear()
        return True


@pytest.mark.asyncio
async def test_followup_starts_worker_and_inject_does_not(tmp_path):
    agent = FakeAgent()
    service = InboxService(InboxRepository(tmp_path), agent)
    await service.start()
    await service.inject(InboundMessage("s1", "ctx", "ws", "context", source="plugin"))
    await asyncio.sleep(0)
    assert agent.received == []

    await service.followup(InboundMessage("s1", "r1", "ws", "hello"))
    await asyncio.wait_for(agent.done.wait(), timeout=1)
    for _ in range(100):
        if len(agent.received) == 2:
            break
        await asyncio.sleep(0.01)
    assert [message.request_id for message in agent.received] == ["ctx", "r1"] or [
        message.request_id for message in agent.received
    ] == ["r1", "ctx"]
    await service.close()


@pytest.mark.asyncio
async def test_queue_snapshot_wire_hides_internal_targets(tmp_path):
    service = InboxService(InboxRepository(tmp_path))
    await service.followup(InboundMessage("s1", "r1", "ws", "hello"))
    wire = await service.wire_snapshot("s1")
    assert wire == {
        "session_id": "s1",
        "items": [{
            "id": "r1",
            "placement": "queued",
            "message": {
                "content": [{"type": "text", "text": "hello"}],
                "attachments": [],
            },
        }],
    }


@pytest.mark.asyncio
async def test_edit_remove_and_promote_only_touch_pending_items(tmp_path):
    service = InboxService(InboxRepository(tmp_path))
    await service.followup(InboundMessage("s1", "r1", "ws", "old"))
    await service.followup(InboundMessage("s1", "r2", "ws", "keep"))
    assert await service.edit("s1", "r1", "new") is True
    assert await service.promote("s1", "r1") is True
    snapshot = await service.snapshot("s1")
    assert [item.request_id for item in snapshot.next_step] == ["r1"]
    assert snapshot.next_step[0].content == "new"
    assert await service.remove("s1", "missing") is False
    assert await service.remove("s1", "r1") is True
    assert await service.remove("s1", "r1") is False
    await service.close()


@pytest.mark.asyncio
async def test_active_reasoning_claims_only_next_step_atomically(tmp_path):
    service = InboxService(InboxRepository(tmp_path))
    await service.steer(InboundMessage("s1", "step-1", "ws", "steer"))
    await service.followup(InboundMessage("s1", "turn-1", "ws", "followup"))

    claimed = await service.claim_next_step_for_reasoning("s1")

    assert [item.request_id for item in claimed] == ["step-1"]
    snapshot = await service.snapshot("s1")
    assert [item.request_id for item in snapshot.pending] == ["turn-1"]
    # Repository.claim 是幂等的：同一 active step 不会重复交付。
    assert await service.claim_next_step_for_reasoning("s1") == ()
    await service.close()


@pytest.mark.asyncio
async def test_steer_does_not_create_unfinishable_receipt(tmp_path):
    service = InboxService(InboxRepository(tmp_path))
    await service.steer(InboundMessage("s1", "step-1", "ws", "steer"))

    assert service._receipts == {}
    with pytest.raises(ValueError, match="followup/next-turn"):
        await service.wait("s1", "step-1")
    await service.close()


@pytest.mark.asyncio
async def test_duplicate_followup_does_not_create_receipt_after_original_completed(tmp_path):
    agent = FakeAgent()
    seen: set[str] = set()
    service = InboxService(
        InboxRepository(tmp_path, request_seen=lambda _session_id, request_id: request_id in seen),
        agent,
    )
    first = await service.followup(InboundMessage("s1", "r1", "ws", "hello"))
    await asyncio.wait_for(agent.done.wait(), timeout=1)
    await asyncio.sleep(0.05)
    assert ("s1", first.request_id) not in service._receipts

    seen.add("r1")
    duplicate = await service.followup(InboundMessage("s1", "r1", "ws", "hello again"))
    assert duplicate.accepted is True
    assert duplicate.created is False
    with pytest.raises(ValueError, match="followup/next-turn"):
        await service.wait("s1", duplicate.request_id)
    await service.close()


@pytest.mark.asyncio
async def test_before_claim_failure_keeps_entire_batch(tmp_path):
    agent = FakeAgent()
    service = InboxService(InboxRepository(tmp_path), agent)
    service._hook_runtime = None

    async def reject(_item, _snapshot):
        return False

    service.bind_before_claim(reject)
    await service.steer(InboundMessage("s1", "step", "ws", "step"))
    await service.followup(InboundMessage("s1", "turn", "ws", "turn"))
    await asyncio.sleep(0.05)
    assert agent.received == []
    assert [item.request_id for item in (await service.snapshot("s1")).pending] == ["step", "turn"]
    await service.close()


@pytest.mark.asyncio
async def test_bus_prompt_accepts_structured_text_content(tmp_path):
    service = InboxService(InboxRepository(tmp_path))
    message = type(
        "BusMessage",
        (),
        {
            "type": "user_message",
            "id": "bus-1",
            "from_channel": "ws",
            "from_session": "s1",
            "metadata": type("Meta", (), {"request_id": "r1", "model_dump": lambda self, **_: {}})(),
            "data": {
                "session_id": "s1",
                "content": [{"type": "text", "text": "你好"}, {"type": "text", "text": "，世界"}],
            },
        },
    )()
    admission = await service.handle_bus_message(message)
    assert admission.accepted is True
    assert (await service.snapshot("s1")).pending[0].content == "你好，世界"
    await service.close()


@pytest.mark.asyncio
async def test_close_releases_pending_memory_but_keeps_recovery_file(tmp_path):
    repository = InboxRepository(tmp_path)
    service = InboxService(repository)
    await service.followup(InboundMessage("s1", "r1", "ws", "hello"))
    assert repository._states
    await service.close()
    assert repository._states == {}
    assert (tmp_path / "s1" / "inbox.json").exists()


@pytest.mark.asyncio
async def test_close_clears_host_callbacks_and_agent_reference(tmp_path):
    agent = FakeAgent()
    service = InboxService(InboxRepository(tmp_path), agent)
    runtime = object()
    service.bind_snapshot_publisher(lambda _session_id: None)
    service.bind_status_publisher(lambda _session_id, _status: None)
    service.bind_before_claim(lambda _item, _snapshot: True)
    service.bind_hook_runtime(runtime)

    await service.close()

    assert service.is_closed is True
    assert service._publish_snapshot is None
    assert service._publish_status is None
    assert service._before_claim is None
    assert service._hook_runtime is None
    assert service._agent is None


@pytest.mark.asyncio
async def test_discard_hook_removes_only_explicit_candidate(tmp_path):
    context = Context()
    runtime = HookRuntime(context)
    agent = FakeAgent()
    service = InboxService(InboxRepository(tmp_path))
    service.bind_hook_runtime(runtime)

    async def policy(payload, next_):
        if payload.candidate.request_id == "drop":
            return RejectClaim("discard", "policy")
        return await next_()

    receipt = runtime.register(
        INBOX_BEFORE_CLAIM_SPEC,
        policy,
        owner="test-policy",
        global_listener=True,
    )
    await service.followup(InboundMessage("s1", "drop", "ws", "drop"))
    await service.followup(InboundMessage("s1", "keep", "ws", "keep"))
    service.attach_agent(agent)
    await service.start()
    for _ in range(100):
        if [item.request_id for item in agent.received] == ["keep"]:
            break
        await asyncio.sleep(0.01)
    assert [item.request_id for item in agent.received] == ["keep"]
    assert not (await service.snapshot("s1")).has_pending
    receipt.dispose()
    cleanup = context.dispose()
    if cleanup is not None:
        await cleanup
    await service.close()


@pytest.mark.asyncio
async def test_cancel_active_preserves_both_pending_targets(tmp_path):
    agent = CancellableAgent()
    service = InboxService(InboxRepository(tmp_path))
    await service.inject(InboundMessage("s1", "context", "ws", "ctx", source="plugin"))
    await service.followup(InboundMessage("s1", "queued", "ws", "queued"))
    service.attach_agent(agent)
    assert await service.cancel("s1") is True
    snapshot = await service.snapshot("s1")
    assert {item.request_id for item in snapshot.pending} == {"context", "queued"}
    await service.close()
