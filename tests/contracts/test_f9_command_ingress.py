"""Command Plane 与 Inbox/Agent Plane 解耦契约。"""

from __future__ import annotations

import asyncio

import pytest

from ftre.app.gateway.composition import build_composition
from ftre.plugins.builtin.command import CommandResult, CommandService
from ftre.services.messaging.bus import MESSAGING_ROUTE_SPEC, BusMessage


def _inbound(content: str, *, session_id: str = "session-1") -> BusMessage:
    return BusMessage(
        type="user_message",
        from_channel="ws",
        to_channel="agent",
        from_session=session_id,
        to_session=session_id,
        data={"session_id": session_id, "content": content},
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


@pytest.mark.asyncio
async def test_submit_inbound_ack_does_not_wait_for_slow_handler() -> None:
    """慢命令不能占住 MessageBus 的 inbound consumer。"""
    service = CommandService()
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    seen: list[str] = []

    async def slow_handler(_ctx):
        seen.append("started")
        started.set()
        await release.wait()
        finished.set()
        return CommandResult.success("done")

    service.register("/slow", slow_handler)
    inbound = _inbound("/slow")
    definition = service.parse({"inbound": inbound})

    assert service.submit_inbound(inbound, definition=definition) is True
    await asyncio.wait_for(started.wait(), timeout=1)
    assert not finished.is_set()
    assert seen == ["started"]

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    await service.close()


@pytest.mark.asyncio
async def test_submit_inbound_is_idempotent_while_request_is_inflight() -> None:
    """客户端重连重发同一 request_id 时不能重复执行命令。"""
    service = CommandService()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_handler(_ctx):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return CommandResult.success()

    service.register("/slow", slow_handler)
    inbound = _inbound("/slow")
    definition = service.parse({"inbound": inbound})
    assert service.submit_inbound(inbound, definition=definition) is True
    assert service.submit_inbound(inbound, definition=definition) is True
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await service.close()
    assert calls == 1


@pytest.mark.asyncio
async def test_messaging_command_route_ack_is_independent_from_handler_runtime(tmp_path) -> None:
    """接入 Hook 只确认命令已接纳，不等待命令本身完成。"""
    composition = await build_composition(
        {
            "sessions_dir": str(tmp_path / "sessions"),
            "plugins": [{"id": "compaction", "enabled": False}],
        }
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(_ctx):
        started.set()
        await release.wait()
        return CommandResult.success("done")

    commands = composition.context.get("commands")
    commands.register("/slow", slow_handler)
    session_id = await composition.context.sessions.create_session("ws")
    inbound = _inbound("/slow", session_id=session_id)
    try:
        result = await asyncio.wait_for(
            composition.context.hook_runtime.dispatch(
                MESSAGING_ROUTE_SPEC,
                inbound,
            ),
            timeout=0.5,
        )
        assert result.accepted is True
        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
    finally:
        await composition.close()
