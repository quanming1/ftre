"""F6.9 command ingress contracts."""

from unittest.mock import AsyncMock

import pytest

from ftre.services.agent_loop.runtime.loop.completion_registry import CompletionRegistry
from ftre.services.agent_loop.runtime.loop.turn_executor import TurnOutcome
from ftre.services.agent_loop.runtime.mailbox.lane import SessionLane
from ftre.services.command import CommandService
from ftre.services.command.types import Handled, SendMessage
from ftre.services.messaging.bus import BusMessage


def _inbound(content: str) -> BusMessage:
    return BusMessage(
        type="user_message",
        from_channel="ws",
        to_channel="agent",
        from_session="session-1",
        to_session="session-1",
        data={"session_id": "session-1", "content": content},
        metadata={},
    )


@pytest.mark.asyncio
async def test_command_service_parses_and_dispatches_at_ingress() -> None:
    service = CommandService()
    seen = []

    async def handler(ctx):
        seen.append((ctx.command, ctx.args, ctx.meta["inbound"].from_session))
        return SendMessage("done")

    service.register("/hello", handler)
    inbound = _inbound("/hello world")

    definition = service.parse({"inbound": inbound})
    result = await service.dispatch_inbound(inbound)

    assert definition is not None and definition.command == "/hello"
    assert isinstance(result, SendMessage)
    assert seen == [("/hello", "world", "session-1")]


@pytest.mark.asyncio
async def test_non_command_is_not_parsed_or_dispatched() -> None:
    service = CommandService()
    called = False

    def handler(_ctx):
        nonlocal called
        called = True
        return Handled()

    service.register("/hello", handler)
    inbound = _inbound("hello")

    assert service.parse({"inbound": inbound}) is None
    assert await service.dispatch_inbound(inbound) is None
    assert called is False


@pytest.mark.asyncio
async def test_session_lane_command_does_not_admit_mailbox_item() -> None:
    service = CommandService()
    service.register("/compact", lambda _ctx: Handled())
    inbound = _inbound("/compact")
    command = service.parse({"inbound": inbound})

    mailbox = type("Mailbox", (), {})()
    mailbox.admit = AsyncMock()
    mailbox.peek = AsyncMock(return_value=None)
    mailbox.advance_revision = AsyncMock()
    executor = type("Executor", (), {})()
    executor.execute_command = AsyncMock(
        return_value=TurnOutcome(turn_id="turn", status="completed")
    )
    publish_snapshot = AsyncMock()
    lane = SessionLane(
        "session-1",
        mailbox=mailbox,
        context_gate=object(),
        executor=executor,
        completion=CompletionRegistry(),
        publish_snapshot=publish_snapshot,
    )

    result = await lane.dispatch_command(inbound, command, service)

    assert result.accepted is True
    mailbox.admit.assert_not_awaited()
    executor.execute_command.assert_awaited_once_with(inbound, command, Handled())
