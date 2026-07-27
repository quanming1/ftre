import sqlite3

import pytest

from ftre.session.converter import to_openai
from ftre.session.manager import SessionManager
from ftre_agent_core.message import (
    AssistantMsg,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def test_persisted_msg_converts_without_event_replay():
    message = AssistantMsg(
        name="assistant",
        content=[
            ThinkingBlock(thinking="internal reasoning"),
            ToolCallBlock(id="call-1", name="read", arguments={"path": "README.md"}),
            ToolResultBlock(
                id="call-1",
                name="read",
                output="contents",
                state="success",
            ),
        ],
        id="reply-1",
    )

    assert to_openai([message]) == [
        {
            "role": "assistant",
            "name": "assistant",
            "content": "",
            "reasoning_content": "internal reasoning",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": '{"path": "README.md"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "contents"},
    ]


@pytest.mark.asyncio
async def test_messages_table_stores_msg_columns_only(tmp_path):
    db_path = tmp_path / "sessions.db"
    manager = SessionManager(str(db_path))
    await manager.init()
    session_id = await manager.create_session("ws")
    await manager.save_message(
        session_id,
        AssistantMsg(name="assistant", content="hello", id="reply-1"),
    )
    await manager.close()

    connection = sqlite3.connect(db_path)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(messages)")
    }
    rows = connection.execute("SELECT role, content FROM messages").fetchall()
    connection.close()

    assert "type" not in columns
    assert "data" not in columns
    assert "reply_id" not in columns
    assert len(rows) == 1
    assert rows[0][0] == "assistant"
    assert '"hello"' in rows[0][1]
