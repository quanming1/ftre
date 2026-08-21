"""F8/F9 Command Plane ingress contracts."""

from unittest.mock import AsyncMock

import pytest

from ftre.services.agent_loop.runtime.loop.completion_registry import CompletionRegistry
from ftre.services.agent_loop.runtime.mailbox.lane import SessionLane
from ftre.services.command import CommandResult, CommandService
from ftre.services.messaging.bus import BusMessage


def _inbound(content: str) -> BusMessage:
    return BusMessage(
        type="user_message",
        from_channel="ws",
        to_channel="agent",
        from_session="session-1",
        to_session="session-1",
        data={"session_id": "session-1", "content": content},
        metadata={"request_id": "request-1"},
    )


@pytest.mark.asyncio
async def test_command_service_parses_and_dispatches_at_ingress() -> None:
    service = CommandService()
    seen = []

    async def handler(ctx):
        seen.append((ctx.command, ctx.args, ctx.session_id, ctx.channel_id))
        return CommandResult.success("done")

    service.register("/hello", handler)
    inbound = _inbound("/hello world")

    definition = service.parse({"inbound": inbound})
    result = await service.dispatch_inbound(inbound, definition=definition)

    assert definition is not None and definition.command == "/hello"
    assert result == CommandResult.success("done")
    assert seen == [("/hello", "world", "session-1", "ws")]


@pytest.mark.asyncio
async def test_non_command_is_not_parsed_or_dispatched() -> None:
    service = CommandService()
    called = False

    def handler(_ctx):
        nonlocal called
        called = True
        return CommandResult.success()

    service.register("/hello", handler)
    inbound = _inbound("hello")

    assert service.parse({"inbound": inbound}) is None
    assert await service.dispatch_inbound(inbound) is None
    assert called is False


@pytest.mark.asyncio
async def test_session_lane_command_does_not_admit_or_execute_turn() -> None:
    service = CommandService()
    service.register("/compact", lambda _ctx: CommandResult.success())
    inbound = _inbound("/compact")
    command = service.parse({"inbound": inbound})

    mailbox = type("Mailbox", (), {})()
    mailbox.admit = AsyncMock()
    mailbox.peek = AsyncMock(return_value=None)
    mailbox.advance_revision = AsyncMock()
    executor = type("Executor", (), {})()
    executor.execute = AsyncMock()
    publish_snapshot = AsyncMock()
    publish_result = AsyncMock()
    lane = SessionLane(
        "session-1",
        mailbox=mailbox,
        context_gate=object(),
        executor=executor,
        completion=CompletionRegistry(),
        publish_snapshot=publish_snapshot,
        publish_command_result=publish_result,
    )

    result = await lane.dispatch_command(inbound, command, service)

    assert result.accepted is True
    mailbox.admit.assert_not_awaited()
    executor.execute.assert_not_awaited()
    publish_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_command_lifecycle_is_paired_without_opening_a_turn() -> None:
    service = CommandService()
    events = []

    async def observe(event_type, payload):
        events.append((event_type, payload))

    service.bind_lifecycle(observe)
    service.register("/hello", lambda _ctx: CommandResult.success("ok"))

    result = await service.dispatch_inbound(_inbound("/hello value"))

    assert result == CommandResult.success("ok")
    assert [event_type for event_type, _ in events] == ["command/run", "command/done"]
    assert events[0][1]["command_id"] == events[1][1]["command_id"]
    assert events[0][1]["args"] == "value"
    assert events[1][1]["kind"] == "success"


@pytest.mark.asyncio
async def test_successful_command_request_id_is_at_most_once() -> None:
    service = CommandService()
    calls = 0

    async def handler(_ctx):
        nonlocal calls
        calls += 1
        return CommandResult.success("once")

    service.register("/once", handler)
    inbound = _inbound("/once")
    first = await service.dispatch_inbound(inbound)
    second = await service.dispatch_inbound(inbound)

    assert first == second == CommandResult.success("once")
    assert calls == 1
