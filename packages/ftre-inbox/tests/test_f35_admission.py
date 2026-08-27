from __future__ import annotations

import asyncio

import pytest
from ftre_agent import (
    AgentRunRequest,
    AgentRunResult,
    AgentRuntimeFactory,
    InboundMessage,
)
from ftre_agent_core.message import UserMsg
from ftre_inbox.models import QueueItem
from ftre_inbox.repository import InboxRepository
from ftre_inbox.service import InboxService


class RuntimeHandle:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    async def run(self, request):
        self.calls.append(request)
        return AgentRunResult(
            session_id=request.session_id,
            turn_id=request.request_id,
            status="completed",
        )

    async def cancel(self, reason=""):
        del reason
        return False

    async def dispose(self):
        return None


class RuntimeFactory(AgentRuntimeFactory):
    name = "test-runtime"
    version = "test"

    def __init__(self) -> None:
        self.calls = []

    async def create(self, spec):
        return RuntimeHandle(self.calls)

    async def resume(self, spec):
        return RuntimeHandle(self.calls)

    async def run_inbound(self, message):
        del message
        raise AssertionError("legacy inbound path must not be used")

    async def cancel_session(self, *args, **kwargs):
        del args, kwargs
        return False

    def get_session_status(self, session_id):
        del session_id
        return "idle"

    def is_active_session(self, session_id):
        del session_id
        return False

    async def delete_session(self, session_id):
        del session_id

    async def resume_confirmation(self, *args, **kwargs):
        del args, kwargs


@pytest.mark.asyncio
async def test_lease_release_and_ack_are_durable(tmp_path):
    repository = InboxRepository(tmp_path)
    await repository.admit(QueueItem("r1", 0, "s1", "ws", "hello"), "next-turn")

    leases = await repository.claim_lease("s1", ("r1",))
    assert len(leases) == 1
    assert (await repository.snapshot("s1")).inflight_count == 1
    await repository.release("s1", leases[0].lease_id)
    assert (await repository.snapshot("s1")).pending[0].request_id == "r1"

    leases = await repository.claim_lease("s1", ("r1",))
    await repository.ack("s1", leases[0].lease_id)
    snapshot = await repository.snapshot("s1")
    assert snapshot.has_pending is False
    assert snapshot.inflight_count == 0


@pytest.mark.asyncio
async def test_orphaned_lease_is_requeued_by_new_repository(tmp_path):
    first = InboxRepository(tmp_path)
    await first.admit(QueueItem("r1", 0, "s1", "ws", "hello"), "next-turn")
    await first.claim_lease("s1", ("r1",))

    replacement = InboxRepository(tmp_path)
    await replacement.load_all()
    snapshot = await replacement.snapshot("s1")
    assert [item.request_id for item in snapshot.pending] == ["r1"]
    assert snapshot.inflight_count == 0


@pytest.mark.asyncio
async def test_structured_agent_path_uses_msg_request_and_reservation(tmp_path):
    from ftre_agent import AgentService

    agents = AgentService()
    factory = RuntimeFactory()
    agents.register_factory(factory)
    service = InboxService(InboxRepository(tmp_path), agents)
    await service.start()

    admitted = await service.followup(InboundMessage("s1", "r1", "ws", "hello"))
    assert admitted.accepted is True
    await asyncio.wait_for(service.wait("s1", "r1"), timeout=1)

    assert len(factory.calls) == 1
    request = factory.calls[0]
    assert request.request_id == "r1"
    assert request.messages[0].role == "user"
    assert request.messages[0].get_text_content() == "hello"
    assert request.agent_id == "s1:default"
    assert agents._reservations == {}
    await service.close()


@pytest.mark.asyncio
async def test_admission_keeps_agent_run_request_messages(tmp_path):
    service = InboxService(InboxRepository(tmp_path))
    request = AgentRunRequest(
        session_id="s1",
        request_id="r1",
        messages=(UserMsg(content="structured"),),
        agent_id="default",
    )
    result = await service.followup(request)
    assert result.accepted is True
    item = (await service.snapshot("s1")).pending[0]
    assert item.messages[0].get_text_content() == "structured"
