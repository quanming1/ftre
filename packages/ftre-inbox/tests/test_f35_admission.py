from __future__ import annotations

import asyncio

import pytest
from ftre_agent import (
    AgentRunRequest,
    AgentRunResult,
    AgentRuntimeFactory,
)
from ftre_agent.message import UserMsg
from ftre_inbox.hooks import INBOX_BEFORE_ADMIT_SPEC, RejectAdmission
from ftre_inbox.models import QueueItem
from ftre_inbox.protocol import InboundMessage
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

    @property
    def control(self):
        return self

    async def create(self, spec):
        return RuntimeHandle(self.calls)

    async def resume(self, spec):
        return RuntimeHandle(self.calls)

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


class FailedRuntimeHandle(RuntimeHandle):
    async def run(self, request):
        self.calls.append(request)
        return AgentRunResult(
            session_id=request.session_id,
            turn_id=request.request_id,
            status="failed",
            error={"code": "temporary", "message": "upstream unavailable", "retryable": True},
        )


class FailedRuntimeFactory(RuntimeFactory):
    async def create(self, spec):
        del spec
        return FailedRuntimeHandle(self.calls)


class CancelledRuntimeHandle(RuntimeHandle):
    async def run(self, request):
        self.calls.append(request)
        return AgentRunResult(
            session_id=request.session_id,
            turn_id=request.request_id,
            status="cancelled",
            error={"code": "cancelled", "message": "user cancelled", "retryable": False},
        )


class CancelledRuntimeFactory(RuntimeFactory):
    async def create(self, spec):
        del spec
        return CancelledRuntimeHandle(self.calls)


class PauseThenCompleteHandle(RuntimeHandle):
    async def run(self, request):
        self.calls.append(request)
        paused = len(self.calls) == 1
        return AgentRunResult(
            session_id=request.session_id,
            turn_id=request.request_id,
            status="completed",
            paused=paused,
        )


class PauseThenCompleteFactory(RuntimeFactory):
    async def create(self, spec):
        del spec
        return PauseThenCompleteHandle(self.calls)


class RejectAdmissionHook:
    async def dispatch(self, spec, payload):
        assert spec is INBOX_BEFORE_ADMIT_SPEC
        assert payload.request_id == "blocked"
        return RejectAdmission("policy rejected")


@pytest.mark.asyncio
async def test_claim_removes_item_durably(tmp_path):
    repository = InboxRepository(tmp_path)
    await repository.admit(QueueItem("r1", 0, "s1", "ws", "hello"), "next-turn")

    assert await repository.claim("s1", ("r1",))
    snapshot = await repository.snapshot("s1")
    assert snapshot.has_pending is False


@pytest.mark.asyncio
async def test_claimed_item_is_not_restored_by_new_repository(tmp_path):
    first = InboxRepository(tmp_path)
    await first.admit(QueueItem("r1", 0, "s1", "ws", "hello"), "next-turn")
    await first.claim("s1", ("r1",))

    replacement = InboxRepository(tmp_path)
    await replacement.load_all()
    snapshot = await replacement.snapshot("s1")
    assert snapshot.pending == ()


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
async def test_paused_run_does_not_consume_queue_until_confirmation_completes(tmp_path):
    from ftre_agent import AgentService

    agents = AgentService()
    factory = PauseThenCompleteFactory()
    agents.register_factory(factory)
    service = InboxService(InboxRepository(tmp_path), agents)
    await service.start()

    first = await service.followup(InboundMessage("s1", "r1", "ws", "first"))
    await asyncio.wait_for(service.wait("s1", first.request_id), timeout=1)
    await service.followup(InboundMessage("s1", "r2", "ws", "queued"))
    await asyncio.sleep(0.05)

    assert [request.request_id for request in factory.calls] == ["r1"]

    await agents.resume_confirmation("s1", "ws", [], {})
    for _ in range(100):
        if [request.request_id for request in factory.calls] == ["r1", "r2"]:
            break
        await asyncio.sleep(0.01)
    assert [request.request_id for request in factory.calls] == ["r1", "r2"]
    assert not (await service.snapshot("s1")).has_pending
    await service.close()


@pytest.mark.asyncio
async def test_agent_failure_after_claim_freezes_queue(tmp_path):
    from ftre_agent import AgentService

    agents = AgentService()
    agents.register_factory(FailedRuntimeFactory())
    service = InboxService(InboxRepository(tmp_path), agents)
    await service.start()

    admitted = await service.followup(InboundMessage("s1", "r1", "ws", "hello"))
    result = await asyncio.wait_for(service.wait("s1", admitted.request_id), timeout=1)

    assert result.status == "failed"
    snapshot = await service.snapshot("s1")
    assert snapshot.pending == ()
    assert service.status("s1") == "blocked"
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_request_is_terminal_before_new_message(tmp_path):
    from ftre_agent import AgentService

    agents = AgentService()
    factory = CancelledRuntimeFactory()
    agents.register_factory(factory)
    service = InboxService(InboxRepository(tmp_path), agents)
    await service.start()

    first = await service.followup(InboundMessage("s1", "r1", "ws", "first"))
    result = await asyncio.wait_for(service.wait("s1", first.request_id), timeout=1)
    assert result.status == "cancelled"

    await service.followup(InboundMessage("s1", "r2", "ws", "second"))
    await asyncio.sleep(0.05)

    assert [request.request_id for request in factory.calls] == ["r1"]
    assert [item.request_id for item in (await service.snapshot("s1")).pending] == ["r2"]
    assert service.status("s1") == "blocked"
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


@pytest.mark.asyncio
async def test_before_admit_hook_rejects_without_persisting_item(tmp_path):
    service = InboxService(
        InboxRepository(tmp_path),
        hook_runtime=RejectAdmissionHook(),
    )

    result = await service.followup(InboundMessage("s1", "blocked", "ws", "hello"))

    assert result.accepted is False
    assert result.error == {
        "code": "admission-rejected",
        "message": "policy rejected",
        "retryable": False,
    }
    assert not (await service.snapshot("s1")).has_pending
    await service.close()
