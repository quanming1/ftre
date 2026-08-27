"""F35.2 AgentService 公共 API 与 Runtime Handle 契约。"""

from __future__ import annotations

import asyncio

import pytest
from ftre_agent import (
    AgentBusyError,
    AgentConfig,
    AgentCreateSpec,
    AgentEvent,
    AgentNotFoundError,
    AgentResumeSpec,
    AgentRunRequest,
    AgentRunResult,
    AgentService,
    AgentView,
    FactoryRegistration,
)
from ftre_agent_core.message import UserMsg


class _Handle:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        self.started.set()
        await self.release.wait()
        return AgentRunResult(
            session_id=request.session_id,
            turn_id="turn-1",
            status="completed",
        )

    async def stream(self, request: AgentRunRequest):
        result = await self.run(request)
        yield AgentEvent(
            event_type="run.completed",
            agent_id="agent-1",
            run_id=result.turn_id,
            sequence=0,
            data={"status": result.status},
        )

    async def cancel(self, reason: str = "") -> bool:
        self.release.set()
        return True

    async def dispose(self) -> None:
        self.release.set()


class _Factory:
    name = "fake-runtime"
    version = "test"

    def __init__(self) -> None:
        self.handles: list[_Handle] = []

    async def create(self, spec: AgentCreateSpec) -> _Handle:
        handle = _Handle()
        self.handles.append(handle)
        return handle

    async def resume(self, spec: AgentResumeSpec) -> _Handle:
        return await self.create(
            AgentCreateSpec(spec.agent_id, spec.config, spec.session_id, spec.metadata)
        )

    async def run_inbound(self, message):
        return AgentRunResult(session_id=message.session_id, turn_id="legacy", status="completed")

    async def cancel_session(self, *args, **kwargs):
        return True

    def get_session_status(self, session_id):
        return "idle"

    def is_active_session(self, session_id):
        return False

    async def delete_session(self, session_id):
        return None

    async def resume_confirmation(self, *args, **kwargs):
        return None


def _request(request_id: str = "request-1") -> AgentRunRequest:
    return AgentRunRequest(
        session_id="session-1",
        request_id=request_id,
        messages=(UserMsg(content="hello"),),
    )


@pytest.mark.asyncio
async def test_create_run_view_and_dispose_use_public_contract() -> None:
    service = AgentService()
    factory = _Factory()
    service.start()
    service.register_factory(factory)

    handle = await service.create(
        AgentCreateSpec(agent_id="agent-1", config=AgentConfig(), session_id="session-1")
    )
    assert isinstance(handle.view(), AgentView)
    assert handle.view().state == "idle"
    factory.handles[0].release.set()
    result = await handle.run(_request())
    assert result.run_id == "request-1"
    assert service.status("agent-1") == "idle"

    await handle.dispose()
    assert service.get("agent-1") is None


@pytest.mark.asyncio
async def test_busy_and_unknown_agent_are_typed_errors() -> None:
    service = AgentService()
    factory = _Factory()
    service.start()
    service.register_factory(factory)
    await service.create(AgentCreateSpec("agent-1", AgentConfig(), "session-1"))
    task = asyncio.create_task(service.run("agent-1", _request()))
    await factory.handles[0].started.wait()
    with pytest.raises(AgentBusyError):
        await service.run("agent-1", _request("request-2"))
    with pytest.raises(AgentNotFoundError):
        await service.run("missing", _request("request-3"))
    await service.cancel("agent-1", reason="test")
    assert (await task).status == "completed"


@pytest.mark.asyncio
async def test_stream_and_resume_are_available_without_runtime_object_leak() -> None:
    service = AgentService()
    factory = _Factory()
    service.start()
    service.register_factory(factory)
    handle = await service.resume(
        AgentResumeSpec("agent-1", "session-1", AgentConfig(), checkpoint_id="cp-1")
    )
    factory.handles[0].release.set()
    events = [event async for event in handle.stream(_request())]
    assert events[0].event_type == "run.completed"
    assert service.get("agent-1").run_id == "request-1"


def test_factory_registration_handle_is_public_and_immutable() -> None:
    assert FactoryRegistration.__dataclass_params__.frozen is True
