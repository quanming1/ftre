"""Command Plane 与 Inbox/Agent Plane 解耦契约。"""

from __future__ import annotations

import pytest

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
async def test_unknown_slash_is_command_plane_error_not_agent_input() -> None:
    service = CommandService()
    inbound = _inbound("/compact")
    assert service.is_command_input({"inbound": inbound}) is True
    assert service.parse({"inbound": inbound}) is None
    result = CommandResult.error("命令不可用或未启用")
    assert result.kind == "error"


@pytest.mark.asyncio
async def test_direct_command_dispatch_stays_inside_command_service() -> None:
    service = CommandService()
    service.register("/hello", lambda _ctx: CommandResult.success("ok"))
    inbound = _inbound("/hello")
    result = await service.dispatch_inbound(
        inbound,
        definition=service.parse({"inbound": inbound}),
    )
    assert result == CommandResult.success("ok")
