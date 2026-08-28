from __future__ import annotations

import pytest
from ftre_agent import (
    AgentConfig,
    AgentCreateSpec,
    AgentRegistry,
    AgentRunRequest,
    AgentRunResult,
    AgentService,
)
from ftre_agent.message import UserMsg


class FakeRuntime:
    """实现 AgentService 约定的 runtime 方法集。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    @property
    def control(self):
        return self

    def is_active_session(self, session_id: str) -> bool:
        return session_id == "busy"

    def get_session_status(self, session_id: str) -> str:
        return "running" if session_id == "busy" else "idle"

    async def create(self, spec):
        return self

    async def resume(self, spec):
        return self

    async def run(self, request):
        self.calls.append(("run", (request,), {}))
        return AgentRunResult(session_id=request.session_id, turn_id=request.request_id, status="completed")

    async def dispose(self):
        return None

    async def cancel_session(self, *args, **kwargs):
        self.calls.append(("cancel", args, kwargs))
        return True

    async def delete_session(self, session_id: str):
        self.calls.append(("delete_session", (session_id,), {}))

    async def resume_confirmation(self, session_id, channel_id, events, metadata):
        self.calls.append(("resume_confirmation", (session_id, channel_id, events, metadata), {}))
        return "resumed"


@pytest.mark.asyncio
async def test_agent_service_uses_factory_registration_and_closes_cleanly():
    service = AgentService()
    runtime = FakeRuntime()
    service.start()
    registration = service.register_factory(runtime)

    assert not hasattr(service, "loop")
    assert service.list() == ()
    assert service.get_session_status("busy") == "running"
    assert service.is_session_busy("busy") is True
    await service.create(AgentCreateSpec("agent", AgentConfig(), "session"))
    result = await service.run(
        "agent",
        AgentRunRequest("session", "request", (UserMsg(content="message"),)),
    )
    assert result.status == "completed"
    assert await service.cancel("session") is True
    assert [call[0] for call in runtime.calls] == ["run", "cancel"]

    assert service.unregister_factory(registration) is True
    assert service.unregister_factory(registration) is False
    assert service.list() == ()
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
