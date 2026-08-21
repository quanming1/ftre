"""工具确认控制指令测试。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ftre_agent_core.message import AssistantMsg, ToolCallBlock, ToolCallState

from ftre.services.command import CommandManager
from ftre.services.command.builtin import register_builtin_commands
from ftre.services.command.types import ResumeAgent, SendMessage


def _context(text: str):
    inbound = SimpleNamespace(
        type="user_message",
        from_session="session-1",
        from_channel="ws",
        data={"session_id": "session-1", "content": text},
    )
    return SimpleNamespace(inbound=inbound)


def _manager(messages):
    loop = SimpleNamespace(
        session_manager=SimpleNamespace(
            get_messages_by_session=AsyncMock(return_value=messages)
        ),
        _active_agents={},
        cancel_session=lambda session_id: False,
    )
    manager = CommandManager()
    register_builtin_commands(manager, loop)
    return manager


@pytest.mark.asyncio
async def test_allow_builds_batch_confirmation_events():
    message = AssistantMsg(
        id="reply-1",
        content=[
            ToolCallBlock(
                id="call-1", name="bash", arguments={},
                state=ToolCallState.ASKING,
            ),
            ToolCallBlock(
                id="call-2", name="read", arguments={},
                state=ToolCallState.ASKING,
            ),
        ],
    )
    result = await _manager([message]).try_dispatch(
        _context("/allow call-1 call-2 call-1")
    )

    assert isinstance(result, ResumeAgent)
    assert [event.tool_call_id for event in result.events] == ["call-1", "call-2"]
    assert all(event.reply_id == "reply-1" for event in result.events)
    assert all(event.approved is True for event in result.events)


@pytest.mark.asyncio
async def test_deny_builds_rejected_confirmation_event():
    message = AssistantMsg(
        id="reply-1",
        content=[
            ToolCallBlock(
                id="call-1", name="bash", arguments={},
                state=ToolCallState.ASKING,
            ),
        ],
    )
    result = await _manager([message]).try_dispatch(_context("/deny call-1"))

    assert isinstance(result, ResumeAgent)
    assert result.events[0].approved is False


@pytest.mark.asyncio
async def test_allow_rejects_unknown_or_non_asking_tools():
    message = AssistantMsg(
        id="reply-1",
        content=[
            ToolCallBlock(
                id="call-1", name="bash", arguments={},
                state=ToolCallState.FINISHED,
            ),
        ],
    )
    manager = _manager([message])

    unknown = await manager.try_dispatch(_context("/allow missing"))
    finished = await manager.try_dispatch(_context("/allow call-1"))

    assert isinstance(unknown, SendMessage)
    assert unknown.level == "error"
    assert isinstance(finished, SendMessage)
    assert finished.level == "warning"


@pytest.mark.asyncio
async def test_allow_rejects_tools_from_different_replies():
    messages = [
        AssistantMsg(
            id="reply-1",
            content=[ToolCallBlock(
                id="call-1", name="bash", arguments={},
                state=ToolCallState.ASKING,
            )],
        ),
        AssistantMsg(
            id="reply-2",
            content=[ToolCallBlock(
                id="call-2", name="bash", arguments={},
                state=ToolCallState.ASKING,
            )],
        ),
    ]
    result = await _manager(messages).try_dispatch(
        _context("/allow call-1 call-2")
    )

    assert isinstance(result, SendMessage)
    assert result.level == "error"
