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
async def test_state_json_stores_msg_without_event_fields(tmp_path):
    """session.json 只存元信息；消息事实是 session.jsonl 事件（whole-value）。"""
    import asyncio

    db_path = tmp_path / "sessions.db"
    manager = SessionManager(str(db_path))
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
    # 等待 write-behind 批窗口（200ms）把事件物化为 session.jsonl
    jsonl_path = tmp_path / "sessions" / session_id / "session.jsonl"
    for _ in range(100):
        if jsonl_path.exists():
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("write-behind 未在超时内落盘 session.jsonl")
    await manager.close()

    meta_path = tmp_path / "sessions" / session_id / "session.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))

    # 元信息与消息事实分栏持久化；session.json 无 messages
    assert set(payload) == {"schema_version", "session", "metadata"}
    assert payload["schema_version"] == 2

    # session.jsonl：header + whole-value 事件，不含流式 Event 字段
    lines = [ln for ln in jsonl_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert json.loads(lines[0]) == {"v": 1, "format": "ftre-session-log"}
    assert len(lines) == 2
    event = json.loads(lines[1])
    assert event["type"] == "assistant/message"
    assert event["seq"] == 0
    assert event["message_id"] == "reply-1"
    stored = event["data"]["message"]
    assert stored["role"] == "assistant"
    assert '"hello"' in json.dumps(stored["content"], ensure_ascii=False)
    assert "TEXT_BLOCK_DELTA" not in "\n".join(lines)
    assert "reply_id" not in "\n".join(lines)
    # 不再创建 SQLite 库
    assert not db_path.exists()
