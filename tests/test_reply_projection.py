from unittest.mock import AsyncMock

import pytest

from ftre.agent.reply_projection import ReplyProjection
from ftre_agent_core.event import (
    ReplyEndEvent,
    ReplyFinishedReason,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockStartEvent,
)


@pytest.mark.asyncio
async def test_projection_owns_msg_snapshot_and_revision():
    sessions = AsyncMock()
    projection = ReplyProjection(sessions)
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
    sessions.save_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_reply_end_persists_final_msg_and_removes_active_snapshot():
    sessions = AsyncMock()
    projection = ReplyProjection(sessions)
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
    assert final.finished_reason == ReplyFinishedReason.COMPLETED
    assert await projection.snapshot(session_id) == []
    sessions.update_message.assert_awaited_once_with(final)
