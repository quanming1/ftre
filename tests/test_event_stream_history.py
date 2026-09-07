import json

import pytest
from ftre_agent.message import (
    AssistantMsg,
    MsgName,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)

from ftre.services.session.message.converter import to_openai
from ftre.services.session.service import SessionService as SessionManager


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
async def test_session_json_stores_complete_msg_snapshot(tmp_path):
    manager = SessionManager(str(tmp_path / "sessions.db"), snapshot_interval_ms=20)
    await manager.init()
    session_id = await manager.create_session("ws")
    await manager.append_event(
        session_id,
        "assistant/message",
        {
            "message": AssistantMsg(
                name=MsgName.DEFAULT, content="hello", id="reply-1"
            ).model_dump(mode="json")
        },
        message_id="reply-1",
    )
    await manager.flush_log(session_id)
    await manager.close()

    directory = tmp_path / "sessions" / session_id
    payload = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 5
    assert payload["messages"][0]["id"] == "reply-1"
    assert payload["messages"][0]["content"][0]["text"] == "hello"
    assert not (directory / "session.jsonl").exists()
    assert "assistant/chunk" not in json.dumps(payload, ensure_ascii=False)
    assert not (tmp_path / "sessions.db").exists()
