from __future__ import annotations

import pytest
from ftre_agent import AgentRegistry, AgentService, InboundMessage


class FakeRuntime:
    """实现 AgentService 约定的 runtime 方法集（run_inbound/cancel_session/…）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def is_active_session(self, session_id: str) -> bool:
        return session_id == "busy"

    def get_session_status(self, session_id: str) -> str:
        return "running" if session_id == "busy" else "idle"

    async def run_inbound(self, message):
        self.calls.append(("run", (message,), {}))
        return "executed"

    async def cancel_session(self, *args, **kwargs):
        self.calls.append(("cancel", args, kwargs))
        return True

    async def delete_session(self, session_id: str):
        self.calls.append(("delete_session", (session_id,), {}))

    async def resume_confirmation(self, session_id, channel_id, events, metadata):
        self.calls.append(("resume_confirmation", (session_id, channel_id, events, metadata), {}))
        return "resumed"


@pytest.mark.asyncio
async def test_agent_service_uses_runtime_binding_and_detaches_cleanly():
    service = AgentService()
    runtime = FakeRuntime()
    service.attach_runtime(runtime)

    assert not hasattr(service, "loop")
    assert service.list() == [{"id": "default", "state": "ready"}]
    assert service.get_session_status("busy") == "running"
    assert service.is_session_busy("busy") is True
    assert await service.run(InboundMessage("session", "request", "ws", "message")) == "executed"
    assert await service.cancel("session") is True
    assert [call[0] for call in runtime.calls] == ["run", "cancel"]

    service.detach_runtime()
    service.detach_runtime()
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
