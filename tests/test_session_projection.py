from unittest.mock import AsyncMock

import pytest

from ftre.agent.session_projection import SessionProjection
from ftre_agent_core.event import UserMessageEvent
from ftre_agent_core.event import (
    CustomEvent,
    RequireUserConfirmEvent,
    ReplyEndEvent,
    ReplyFinishedReason,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UserConfirmResultEvent,
)
from ftre_agent_core.message import (
    AssistantMsg,
    ToolCallBlock,
    ToolCallState,
)


@pytest.mark.asyncio
async def test_projection_owns_msg_snapshot_and_revision():
    sessions = AsyncMock()
    projection = SessionProjection(sessions)
    session_id = "ws_sess_test"
    reply_id = "reply_test"

    await projection.apply(session_id, ReplyStartEvent(
        session_id=session_id, reply_id=reply_id, name="assistant",
    ))
    await projection.apply(session_id, TextBlockStartEvent(
        reply_id=reply_id, block_id="text-1",
    ))
    await projection.apply(session_id, TextBlockDeltaEvent(
        reply_id=reply_id, block_id="text-1", delta="hello",
    ))

    snapshots = await projection.snapshot(session_id)
    assert len(snapshots) == 1
    assert snapshots[0]["reply_id"] == reply_id
    assert snapshots[0]["revision"] == 3
    assert snapshots[0]["message"]["content"][0]["text"] == "hello"
    assert snapshots[0]["message"]["name"] == "default"
    assert snapshots[0]["message"]["metadata"]["model"] == "assistant"
    sessions.save_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_message_event_projects_complete_user_msg():
    sessions = AsyncMock()
    projection = SessionProjection(sessions)
    event = UserMessageEvent(
        reply_id="turn_test",
        data={"content": "hello"},
        content=[{"type": "text", "text": "hello"}],
        message_metadata={"hide": False, "agent_id": "default"},
    )

    result = await projection.apply("ws_sess_test", event)

    assert len(result.persisted_messages) == 1
    message = result.persisted_messages[0]
    assert message.id == event.id
    assert message.role == "user"
    assert message.name == "default"
    assert message.get_text_content() == "hello"
    sessions.upsert_message.assert_awaited_once_with("ws_sess_test", message)


@pytest.mark.asyncio
async def test_reply_end_persists_final_msg_and_removes_active_snapshot():
    sessions = AsyncMock()
    projection = SessionProjection(sessions)
    session_id = "ws_sess_test"
    reply_id = "reply_test"

    await projection.apply(session_id, ReplyStartEvent(
        session_id=session_id, reply_id=reply_id, name="assistant",
    ))
    final = await projection.apply(session_id, ReplyEndEvent(
        session_id=session_id,
        reply_id=reply_id,
        finished_reason=ReplyFinishedReason.COMPLETED,
    ))

    assert final is not None
    assert final.completed_message is not None
    assert final.completed_message.finished_reason == ReplyFinishedReason.COMPLETED
    assert await projection.snapshot(session_id) == []
    sessions.update_message.assert_awaited_once_with(final.completed_message)


@pytest.mark.asyncio
async def test_compact_start_is_memory_only_until_terminal_event():
    sessions = AsyncMock()
    projection = SessionProjection(sessions)
    session_id = "ws_sess_test"
    start = CustomEvent(
        name="context_compact_start",
        value={"messages": 10, "tokens": 2000},
    )

    await projection.apply(session_id, start)

    assert await projection.session_event_snapshot(session_id) == [
        start.model_dump(mode="json")
    ]
    sessions.save_message.assert_not_awaited()
    sessions.upsert_message.assert_not_awaited()

    await projection.apply(session_id, CustomEvent(
        name="context_compact_failed",
        value={"reason": "cancelled"},
    ))
    assert await projection.session_event_snapshot(session_id) == []


@pytest.mark.asyncio
async def test_compact_done_persists_and_clears_active_state():
    sessions = AsyncMock()
    projection = SessionProjection(sessions)
    session_id = "ws_sess_test"
    await projection.apply(session_id, CustomEvent(
        name="context_compact_start", value={"tokens": 2000},
    ))

    await projection.apply(session_id, CustomEvent(
        name="context_compact_done",
        value={"mode": "summary", "summary_text": "summary"},
    ))

    sessions.upsert_message.assert_awaited_once()
    assert await projection.session_event_snapshot(session_id) == []


@pytest.mark.asyncio
async def test_user_confirmation_result_checkpoints_tool_call_state():
    sessions = AsyncMock()
    projection = SessionProjection(sessions)
    session_id = "ws_sess_test"
    reply_id = "reply_test"
    call_id = "call-1"

    await projection.apply(session_id, ReplyStartEvent(
        session_id=session_id, reply_id=reply_id, name="assistant",
    ))
    await projection.apply(session_id, ToolCallStartEvent(
        reply_id=reply_id, tool_call_id=call_id, tool_call_name="bash",
    ))
    await projection.apply(session_id, ToolCallEndEvent(
        reply_id=reply_id,
        tool_call_id=call_id,
        tool_call_name="bash",
        arguments={"command": "pwd"},
    ))
    await projection.apply(session_id, RequireUserConfirmEvent(
        reply_id=reply_id,
        tool_call_id=call_id,
        tool_call_name="bash",
        arguments={"command": "pwd"},
        reason="confirm",
    ))
    sessions.update_message.reset_mock()

    await projection.apply(session_id, UserConfirmResultEvent(
        reply_id=reply_id,
        tool_call_id=call_id,
        approved=True,
    ))

    checkpoint = sessions.update_message.await_args.args[0]
    tool_call = next(block for block in checkpoint.content if block.type == "tool_call")
    assert tool_call.state == ToolCallState.ALLOWED


@pytest.mark.asyncio
async def test_confirmation_rehydrates_paused_reply_after_gateway_restart():
    reply_id = "reply_paused"
    persisted = AssistantMsg(
        id=reply_id,
        content=[
            ToolCallBlock(
                id="call-1",
                name="bash",
                arguments={"command": "pwd"},
                state=ToolCallState.ASKING,
            )
        ],
    )
    sessions = AsyncMock()
    sessions.get_messages_by_session.return_value = [
        persisted.model_dump(mode="json")
    ]
    projection = SessionProjection(sessions)

    await projection.apply("ws_sess_test", UserConfirmResultEvent(
        reply_id=reply_id,
        tool_call_id="call-1",
        approved=True,
    ))

    sessions.save_message.assert_not_awaited()
    checkpoint = sessions.update_message.await_args.args[0]
    assert checkpoint.id == reply_id
    assert checkpoint.content[0].state == ToolCallState.ALLOWED
