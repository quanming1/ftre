from __future__ import annotations

import pytest

from ftre.services.agent import AgentRegistry, AgentService


class FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def is_session_busy(self, session_id: str) -> bool:
        return session_id == "busy"

    def get_session_status(self, session_id: str) -> str:
        return "running" if session_id == "busy" else "idle"

    async def submit(self, *args, **kwargs):
        self.calls.append(("submit", args, kwargs))
        return "submitted"

    async def cancel(self, *args, **kwargs):
        self.calls.append(("cancel", args, kwargs))
        return True

    async def wait(self, *args, **kwargs):
        self.calls.append(("wait", args, kwargs))
        return "done"

    async def delete_session(self, session_id: str):
        self.calls.append(("delete_session", (session_id,), {}))

    async def cancel_queued_message(self, session_id: str, request_id: str):
        self.calls.append(("cancel_queued_message", (session_id, request_id), {}))
        return "cancelled"

    async def get_mailbox_snapshot(self, session_id: str):
        self.calls.append(("get_mailbox_snapshot", (session_id,), {}))
        return {"session_id": session_id}

    async def resume_confirmation(self, session_id, channel_id, events, metadata):
        self.calls.append(("resume_confirmation", (session_id, channel_id, events, metadata), {}))
        return "resumed"

    async def wait_session_quiescent(self, session_id):
        self.calls.append(("wait_session_quiescent", (session_id,), {}))


@pytest.mark.asyncio
async def test_agent_service_uses_explicit_driver_port_and_detaches_cleanly():
    service = AgentService()
    driver = FakeDriver()
    service.attach_driver(driver)

    assert not hasattr(service, "loop")
    assert service.list() == [{"id": "default", "state": "ready"}]
    assert service.get_session_status("busy") == "running"
    assert service.is_session_busy("busy") is True
    assert await service.submit("message") == "submitted"
    assert await service.cancel("session") is True
    assert await service.wait("session", "request") == "done"
    assert await service.get_mailbox_snapshot("session") == {"session_id": "session"}
    assert [call[0] for call in driver.calls] == ["submit", "cancel", "wait", "get_mailbox_snapshot"]

    service.detach_driver()
    service.detach_driver()
    assert service.list() == []
    assert service.status("busy") == "idle"


def test_agent_registry_rebuilds_identity_after_dispose():
    registry = AgentRegistry()
    first = registry.register("worker")
    first_identity = first.identity
    assert registry.get("worker") == {"id": "worker", "state": "ready"}
    assert registry.tool_scope("worker") == "agent:worker"
    assert registry.dispose("worker") is True
    second = registry.register("worker")
    assert second.identity is not first_identity
