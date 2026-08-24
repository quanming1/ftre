"""F12 回归：Inbox 独立承担 next-turn/next-step 双队列调度。"""

from __future__ import annotations

import asyncio

import pytest
from ftre_inbox.repository import InboxRepository
from ftre_inbox.service import InboxService

from ftre.services.agent.contracts import InboundMessage


class _Agent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def is_busy(self, _session_id: str) -> bool:
        return False

    async def run(self, message: InboundMessage):
        self.calls.append(message.request_id)
        return message.request_id

    async def cancel(self, _session_id: str):
        return False


@pytest.mark.asyncio
async def test_inbox_claims_all_next_step_and_one_next_turn(tmp_path):
    service = InboxService(InboxRepository(tmp_path / "inbox"))
    await service.steer(InboundMessage("s1", "step-1", "ws", "steer"))
    await service.steer(InboundMessage("s1", "step-2", "ws", "steer 2"))
    await service.followup(InboundMessage("s1", "turn-1", "ws", "followup"))
    await service.followup(InboundMessage("s1", "turn-2", "ws", "later"))
    snapshot = await service.snapshot("s1")
    candidates = service._candidate_batch(snapshot)
    assert [item.request_id for item in candidates] == ["step-1", "step-2", "turn-1"]
    # 第二个 next-turn 不会进入同一批候选。
    assert [item.request_id for item in snapshot.pending] == ["step-1", "step-2", "turn-1", "turn-2"]
    await service.close()


@pytest.mark.asyncio
async def test_inject_idle_does_not_start_worker(tmp_path):
    agent = _Agent()
    service = InboxService(InboxRepository(tmp_path / "inbox"), agent)
    await service.inject(InboundMessage("s1", "context-1", "ws", "context", source="plugin"))
    await asyncio.sleep(0)
    assert agent.calls == []
    assert (await service.snapshot("s1")).has_pending
    await service.close()
