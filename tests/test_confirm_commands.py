"""工具确认控制指令测试。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ftre_agent_core.message import AssistantMsg, ToolCallBlock, ToolCallState

from ftre.plugins.builtin.command import CommandService
from ftre.plugins.builtin.command.builtin import register_builtin_commands
from ftre.services.messaging.bus import BusMessage


def _inbound(text: str) -> BusMessage:
    return BusMessage(
        type="user_message",
        from_session="session-1",
        from_channel="ws",
        to_session="session-1",
        to_channel="agent",
        data={"session_id": "session-1", "content": text},
        metadata={"request_id": "command-1"},
    )


def _service(messages):
    agents = SimpleNamespace(
        resume_confirmation=AsyncMock(),
        cancel=AsyncMock(),
    )
    sessions = SimpleNamespace(
        get_messages_by_session=AsyncMock(return_value=messages),
        fork_session=AsyncMock(),
    )
    service = CommandService()
    register_builtin_commands(
        service.runtime,
        agents=agents,
        sessions=sessions,
    )
    return service, agents


@pytest.mark.asyncio
async def test_allow_builds_batch_confirmation_events():
    message = AssistantMsg(
        id="reply-1",
        content=[
            ToolCallBlock(id="call-1", name="bash", arguments={}, state=ToolCallState.ASKING),
            ToolCallBlock(id="call-2", name="read", arguments={}, state=ToolCallState.ASKING),
        ],
    )
    service, agents = _service([message])
    result = await service.dispatch_inbound(_inbound("/allow call-1 call-2 call-1"))

    assert result is not None and result.kind == "success"
    events = agents.resume_confirmation.await_args.args[2]
    assert [event.tool_call_id for event in events] == ["call-1", "call-2"]
    assert all(event.reply_id == "reply-1" for event in events)
    assert all(event.approved is True for event in events)


@pytest.mark.asyncio
async def test_deny_builds_rejected_confirmation_event():
    message = AssistantMsg(
        id="reply-1",
        content=[ToolCallBlock(id="call-1", name="bash", arguments={}, state=ToolCallState.ASKING)],
    )
    service, agents = _service([message])
    result = await service.dispatch_inbound(_inbound("/deny call-1"))

    assert result is not None and result.kind == "success"
    event = agents.resume_confirmation.await_args.args[2][0]
    assert event.approved is False


@pytest.mark.asyncio
async def test_allow_rejects_unknown_or_non_asking_tools():
    message = AssistantMsg(
        id="reply-1",
        content=[ToolCallBlock(id="call-1", name="bash", arguments={}, state=ToolCallState.FINISHED)],
    )
    service, _ = _service([message])

    unknown = await service.dispatch_inbound(_inbound("/allow missing"))
    finished = await service.dispatch_inbound(_inbound("/allow call-1"))

    assert unknown is not None and unknown.kind == "error"
    assert finished is not None and finished.kind == "error"


@pytest.mark.asyncio
async def test_allow_rejects_tools_from_different_replies():
    messages = [
        AssistantMsg(
            id="reply-1",
            content=[ToolCallBlock(id="call-1", name="bash", arguments={}, state=ToolCallState.ASKING)],
        ),
        AssistantMsg(
            id="reply-2",
            content=[ToolCallBlock(id="call-2", name="bash", arguments={}, state=ToolCallState.ASKING)],
        ),
    ]
    service, agents = _service(messages)
    result = await service.dispatch_inbound(_inbound("/allow call-1 call-2"))

    assert result is not None and result.kind == "error"
    agents.resume_confirmation.assert_not_awaited()
