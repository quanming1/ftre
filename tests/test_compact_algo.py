from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ftre.agent.compact_manager import (
    CompactManager,
    _build_prompt,
    _estimate_body_chars,
    _serialize_messages,
    get_cursor_index,
    get_previous_summary,
)
from ftre.session.converter import to_openai
from ftre_agent_core.message import (
    AssistantMsg,
    SystemMsg,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserMsg,
)


def _record(message, timestamp=1.0):
    return {
        **message.model_dump(mode="json"),
        "session_id": "ws::session",
        "timestamp": timestamp,
    }


def test_summary_cursor_and_previous_summary_use_msg_metadata():
    messages = [
        _record(UserMsg(name="user", content="before"), 1),
        _record(
            SystemMsg(
                name="context_compact",
                content="summary v1",
                metadata={"context_compact": {"mode": "summary"}},
            ),
            2,
        ),
        _record(UserMsg(name="user", content="after"), 3),
    ]
    assert get_cursor_index(messages) == 2
    assert get_previous_summary(messages) == "summary v1"


def test_converter_resets_context_at_summary_msg():
    messages = [
        _record(UserMsg(name="user", content="discarded"), 1),
        _record(
            SystemMsg(
                name="context_compact",
                content="kept summary",
                metadata={"context_compact": {"mode": "summary"}},
            ),
            2,
        ),
        _record(UserMsg(name="user", content="new request"), 3),
    ]
    assert to_openai(messages) == [
        {"role": "user", "content": "[历史上下文摘要]\nkept summary"},
        {"role": "user", "content": [{"type": "text", "text": "new request"}], "name": "user"},
    ]


def test_serialize_messages_includes_tool_call_and_result():
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(id="call-1", name="read", arguments={"path": "a.txt"}),
            ToolResultBlock(
                id="call-1",
                name="read",
                output=[TextBlock(text="file contents")],
                state="success",
            ),
        ],
    )
    output = _serialize_messages([_record(assistant)])
    assert "[Assistant tool call]: read(" in output
    assert "[Tool result]: file contents" in output


@pytest.mark.asyncio
async def test_fast_compact_updates_tool_result_blocks_without_marker_message():
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ToolCallBlock(id=f"call-{index}", name="read", arguments={})
            for index in range(4)
        ]
        + [
            ToolResultBlock(
                id=f"call-{index}",
                name="read",
                output=f"large output {index}",
                state="success",
            )
            for index in range(4)
        ],
    )
    session_manager = AsyncMock()
    session_manager.get_messages_by_session.return_value = [_record(assistant)]
    bus = AsyncMock()
    manager = CompactManager(session_manager=session_manager, bus=bus)

    changed = await manager.compress_fast(
        "ws::session",
        "ws",
        config=SimpleNamespace(),
        keep_recent=1,
    )

    assert changed is True
    session_manager.save_message.assert_not_called()
    session_manager.update_message.assert_awaited_once()
    updated = session_manager.update_message.await_args.args[0]
    results = [
        block for block in updated.content if isinstance(block, ToolResultBlock)
    ]
    assert [result.output[0].text for result in results[:3]] == [
        "[工具输出已压缩]",
        "[工具输出已压缩]",
        "[工具输出已压缩]",
    ]
    assert results[-1].output == "large output 3"


def test_build_prompt_and_body_estimate():
    text = "[User]: 请修复数据库\n\n[Assistant]: 已完成分析"
    assert _estimate_body_chars(text) > 0
    prompt = _build_prompt(previous_summary="old", context=[text], min_chars=200)
    assert "<conversation>" in prompt[0]
    assert "<previous-summary>" in prompt[1]
    assert "更新锚定摘要" in prompt[-1]
