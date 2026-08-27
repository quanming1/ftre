from unittest.mock import AsyncMock

import pytest
from ftre_agent_core.event import (
    CustomEvent,
    ReplyEndEvent,
    ReplyFinishedReason,
    ReplyStartEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UserConfirmResultEvent,
    UserMessageEvent,
)
from ftre_agent_core.message import (
    AssistantMsg,
    ToolCallBlock,
    ToolCallState,
)

from ftre.services.session.projection import SessionProjection
from ftre.services.session.service import SessionService


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
async def test_message_id_keeps_assistant_user_assistant_order(tmp_path):
    """Core 提供新 message_id 后，Projection 只按 id 投影 A→User→B。"""
    sessions = SessionService(sessions_dir=str(tmp_path / "sessions"))
    await sessions.init()
    session_id = await sessions.create_session("ws")
    projection = sessions.projection
    reply_id = "reply-steer"

    await projection.apply(session_id, ReplyStartEvent(
        session_id=session_id, reply_id=reply_id, message_id="assistant-a", name="assistant",
    ))
    await projection.apply(session_id, TextBlockStartEvent(
        reply_id=reply_id, message_id="assistant-a", block_id="before",
    ))
    await projection.apply(session_id, TextBlockDeltaEvent(
        reply_id=reply_id, message_id="assistant-a", block_id="before", delta="前半段",
    ))
    steer = UserMessageEvent(
        id="user-steer-boundary",
        reply_id="input-steer-boundary",
        data={
            "session_id": session_id,
            "request_id": "request-steer-boundary",
            "content": "插入下一步",
        },
        content=[{"type": "text", "text": "插入下一步"}],
        message_metadata={
            "hide": False,
            "request_id": "request-steer-boundary",
            "previous_assistant_message_id": "assistant-a",
        },
    )
    await projection.apply(session_id, steer)
    assert await projection.snapshot(session_id) == []
    await projection.apply(session_id, TextBlockStartEvent(
        reply_id=reply_id, message_id="assistant-b", block_id="after",
    ))
    await projection.apply(session_id, TextBlockDeltaEvent(
        reply_id=reply_id, message_id="assistant-b", block_id="after", delta="后半段",
    ))
    await projection.apply(session_id, TextBlockEndEvent(
        reply_id=reply_id, message_id="assistant-b", block_id="after",
    ))

    messages = await sessions.get_messages_by_session(session_id)
    assert [message["role"] for message in messages] == ["assistant", "user", "assistant"]
    assert messages[0]["content"][0]["text"] == "前半段"
    assert messages[0]["finished_at"] is not None
    assert messages[1]["content"][0]["text"] == "插入下一步"
    assert messages[2]["content"][0]["text"] == "后半段"
    assert [message["id"] for message in messages] == [
        "assistant-a", "user-steer-boundary", "assistant-b",
    ]
    await sessions.close()


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
async def test_reply_end_keeps_snapshot_when_final_persist_fails():
    """最终快照写盘失败时保留 active Reply，供取消收尾重试而不丢消息。"""
    sessions = AsyncMock()
    projection = SessionProjection(sessions)
    session_id = "ws_sess_test"
    reply_id = "reply_test"

    await projection.apply(session_id, ReplyStartEvent(
        session_id=session_id, reply_id=reply_id, name="assistant",
    ))
    sessions.update_message.side_effect = ValueError("session deleted")

    with pytest.raises(ValueError, match="session deleted"):
        await projection.apply(session_id, ReplyEndEvent(
            session_id=session_id,
            reply_id=reply_id,
            finished_reason=ReplyFinishedReason.COMPLETED,
        ))

    snapshot = await projection.snapshot(session_id)
    assert len(snapshot) == 1
    assert snapshot[0]["reply_id"] == reply_id

    sessions.update_message.side_effect = None
    completed = await projection.finish_open(
        session_id, ReplyFinishedReason.ERROR,
        error={"code": "persist_retry"},
    )
    assert [message.id for message in completed] == [reply_id]


@pytest.mark.asyncio
async def test_user_boundary_keeps_assistant_snapshot_when_close_persist_fails():
    sessions = AsyncMock()
    projection = SessionProjection(sessions)
    session_id = "ws_sess_boundary_failure"
    await projection.apply(
        session_id,
        ReplyStartEvent(
            session_id=session_id,
            reply_id="reply-1",
            message_id="assistant-1",
            name="assistant",
        ),
    )
    sessions.update_message.side_effect = ValueError("write failed")

    with pytest.raises(ValueError, match="write failed"):
        await projection.apply(
            session_id,
            UserMessageEvent(
                id="user-1",
                reply_id="turn-1",
                content=[{"type": "text", "text": "next"}],
                message_metadata={"previous_assistant_message_id": "assistant-1"},
            ),
        )

    snapshot = await projection.snapshot(session_id)
    assert snapshot and snapshot[0]["message_id"] == "assistant-1"


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
        arguments='{"command":"pwd"}',
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
