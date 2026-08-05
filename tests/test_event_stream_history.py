import json

import pytest

from ftre.session.message.converter import to_openai

from ftre.session import SessionManager
from ftre_agent_core.message import (
    AssistantMsg,
    MsgName,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def test_persisted_msg_converts_without_event_replay():
    message = AssistantMsg(
        name=MsgName.DEFAULT,
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
async def test_state_json_stores_msg_without_event_fields(tmp_path):
    db_path = tmp_path / "sessions.db"
    manager = SessionManager(str(db_path))
    await manager.init()
    session_id = await manager.create_session("ws")
    await manager.save_message(
        session_id,
        AssistantMsg(name=MsgName.DEFAULT, content="hello", id="reply-1"),
    )
    await manager.close()

    state_path = tmp_path / "sessions" / session_id / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))

    # 根结构只有四个字段
    assert set(payload) == {
        "schema_version",
        "session",
        "messages",
        "metadata",
    }
    assert payload["schema_version"] == 1
    # messages[] 是完整 Msg 快照，不含流式 Event 字段
    assert len(payload["messages"]) == 1
    stored = payload["messages"][0]
    assert stored["role"] == "assistant"
    assert '"hello"' in json.dumps(stored["content"], ensure_ascii=False)
    assert "type" not in stored
    assert "data" not in stored
    assert "reply_id" not in stored
    # 不再创建 SQLite 库
    assert not db_path.exists()
