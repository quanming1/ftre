"""派生消息 + session.json 的一致性分页视图测试（PRD-F43）。

消息经 append_event("user/message" / "assistant/message", whole-value) 提交；
分页视图基于 derived messages，file_path 指向 session.jsonl。
"""

import pytest
import pytest_asyncio
from ftre_agent.message import AssistantMsg, UserMsg

from ftre.services.session.service import SessionService as SessionManager


@pytest_asyncio.fixture
async def manager(tmp_path):
    mgr = SessionManager(sessions_dir=str(tmp_path / "sessions"))
    await mgr.init()
    yield mgr
    await mgr.close()


async def _append_user(manager, session_id, text: str, message_id: str) -> None:
    user = UserMsg(name="default", content=text)
    await manager.append_event(
        session_id,
        "user/message",
        {
            "content": user.model_dump(mode="json")["content"],
            "metadata": {},
            "request_id": message_id,
        },
        message_id=message_id,
    )


async def _append_assistant(manager, session_id, msg: AssistantMsg) -> None:
    await manager.append_event(
        session_id,
        "assistant/message",
        {"message": msg.model_dump(mode="json")},
        message_id=msg.id,
    )


@pytest.mark.asyncio
async def test_state_page_defaults_to_tail_and_supports_earlier_pages(manager, tmp_path):
    session_id = await manager.create_session(channel_id="ws", title="分页测试")
    for index in range(7):
        await _append_user(manager, session_id, f"消息 {index}", f"msg_{index}")

    tail = await manager.get_state_page(session_id, limit=3)
    assert tail is not None
    assert tail["schema_version"] == 2
    assert tail["file_path"] == str(tmp_path / "sessions" / session_id / "session.jsonl")
    assert tail["session"]["id"] == session_id
    assert tail["truncated_message_ids"] == []
    assert tail["stats"]["message_count"] == 7
    assert tail["stats"]["user_messages"] == 7
    assert tail["stats"]["text_blocks"] == 7
    assert [message["id"] for message in tail["messages"]] == [
        "msg_4", "msg_5", "msg_6",
    ]
    assert tail["page"] == {
        "offset": 4,
        "limit": 3,
        "total": 7,
        "has_more_before": True,
        "has_more_after": False,
    }

    earlier = await manager.get_state_page(session_id, offset=1, limit=3)
    assert earlier is not None
    assert [message["id"] for message in earlier["messages"]] == [
        "msg_1", "msg_2", "msg_3",
    ]
    assert earlier["page"]["has_more_before"] is True
    assert earlier["page"]["has_more_after"] is True


@pytest.mark.asyncio
async def test_state_page_clamps_limit_and_returns_none_for_missing_session(manager):
    session_id = await manager.create_session(channel_id="ws")

    page = await manager.get_state_page(session_id, offset=-10, limit=1000)
    assert page is not None
    assert page["page"]["offset"] == 0
    assert page["page"]["limit"] == 100
    assert await manager.get_state_page("ws_sess_missing") is None


@pytest.mark.asyncio
async def test_state_page_truncates_large_strings_and_loads_full_message_on_demand(manager):
    session_id = await manager.create_session(channel_id="ws")
    large_text = "x" * 5_000
    await _append_user(manager, session_id, large_text, "msg_large")

    page = await manager.get_state_page(
        session_id,
        max_string_chars=1_000,
    )
    assert page is not None
    assert page["truncated_message_ids"] == ["msg_large"]
    preview = page["messages"][0]["content"][0]["text"]
    assert len(preview) < len(large_text)
    assert "省略" in preview

    full = await manager.get_state_message(session_id, "msg_large")
    assert full is not None
    assert full["content"][0]["text"] == large_text


@pytest.mark.asyncio
async def test_state_page_stats_cover_full_session_not_only_current_page(manager):
    session_id = await manager.create_session(channel_id="ws")
    await _append_user(manager, session_id, "问题", "msg_user")
    await _append_assistant(
        manager,
        session_id,
        AssistantMsg(
            name="default",
            content="回答",
            id="msg_assistant",
            metadata={"model": "test-model"},
            token={
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
                "last_call_usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            },
        ),
    )

    page = await manager.get_state_page(session_id, limit=1)
    assert page is not None
    assert page["stats"] == {
        "message_count": 2,
        "user_messages": 1,
        "assistant_messages": 1,
        "system_messages": 0,
        "text_blocks": 2,
        "thinking_blocks": 0,
        "tool_calls": 0,
        "tool_results": 0,
        "data_blocks": 0,
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
        "model": "test-model",
    }
