from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ftre.agent.compact_manager import (
    CompactManager,
    _build_prompt,
    _estimate_body_chars,
    _serialize_messages,
)
from ftre.session.message.converter import to_openai
from ftre_agent_core.event import CustomEvent
from ftre_agent_core.message import (
    AssistantMsg,
    MsgName,
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


def test_converter_translates_compact_msg_to_summary_user_message():
    """compact Msg 转为带前缀的 user 消息；不再 messages.clear。"""
    messages = [
        _record(
            UserMsg(
                name=MsgName.COMPACT,
                content="kept summary",
                metadata={"hide": True, "context_compact": {"mode": "summary"}},
            ),
            2,
        ),
        _record(UserMsg(name=MsgName.DEFAULT, content="new request"), 3),
    ]
    result = to_openai(messages)
    assert result == [
        {"role": "user", "content": "[历史上下文摘要]\nkept summary"},
        {"role": "user", "content": [{"type": "text", "text": "new request"}]},
    ]


def test_serialize_messages_includes_tool_call_and_result():
    assistant = AssistantMsg(
        name=MsgName.DEFAULT,
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
async def test_fast_compact_updates_tool_result_blocks_via_emit_event():
    # 构造带 turn 边界的对话：user0 → assistant0(4 个工具结果)
    user = UserMsg(name=MsgName.DEFAULT, content="请读取文件")
    assistant = AssistantMsg(
        name=MsgName.DEFAULT,
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
    records = [_record(user, 1), _record(assistant, 2)]
    session_manager = AsyncMock()
    session_manager.get_context_messages.return_value = records
    emitted: list = []

    async def emit_event(session_id, channel_id, event):
        emitted.append(event)

    manager = CompactManager(session_manager=session_manager, emit_event=emit_event)

    # keep_turns=0（默认）：不保护任何轮，活跃区间内全部工具结果被裁
    changed = await manager.compress_fast(
        "ws::session",
        "ws",
        config=SimpleNamespace(),
        keep_turns=0,
    )

    assert changed is True
    session_manager.save_message.assert_not_called()
    session_manager.update_message.assert_awaited_once()
    updated = session_manager.update_message.await_args.args[0]
    results = [
        block for block in updated.content if isinstance(block, ToolResultBlock)
    ]
    # 全部 4 个工具结果被裁剪
    assert [result.output[0].text for result in results] == [
        "[工具输出已压缩]",
        "[工具输出已压缩]",
        "[工具输出已压缩]",
        "[工具输出已压缩]",
    ]
    # done 事件经统一出口派发
    assert any(
        isinstance(e, CustomEvent) and e.name == "context_compact_done"
        and e.value.get("mode") == "fast"
        for e in emitted
    )


@pytest.mark.asyncio
async def test_fast_compact_keep_turns_protects_recent_turns():
    """keep_turns=1 保护最近一轮（user 边界）内的工具输出不被裁剪。"""
    # turn 0：user0 → assistant0(工具结果 old)
    user0 = UserMsg(name=MsgName.DEFAULT, content="第一轮")
    assistant0 = AssistantMsg(
        name=MsgName.DEFAULT,
        content=[
            ToolCallBlock(id="c-old", name="read", arguments={}),
            ToolResultBlock(id="c-old", name="read", output="old output", state="success"),
        ],
    )
    # turn 1：user1 → assistant1(工具结果 recent)
    user1 = UserMsg(name=MsgName.DEFAULT, content="第二轮")
    assistant1 = AssistantMsg(
        name=MsgName.DEFAULT,
        content=[
            ToolCallBlock(id="c-recent", name="read", arguments={}),
            ToolResultBlock(id="c-recent", name="read", output="recent output", state="success"),
        ],
    )
    records = [
        _record(user0, 1), _record(assistant0, 2),
        _record(user1, 3), _record(assistant1, 4),
    ]
    session_manager = AsyncMock()
    session_manager.get_context_messages.return_value = records
    emitted: list = []

    async def emit_event(session_id, channel_id, event):
        emitted.append(event)

    manager = CompactManager(session_manager=session_manager, emit_event=emit_event)

    # keep_turns=1：保护最近一轮（user1 及之后）——recent 保留，old 被裁
    changed = await manager.compress_fast(
        "ws::session", "ws", config=SimpleNamespace(), keep_turns=1,
    )

    assert changed is True
    # 只更新了 assistant0（含 old 工具结果），assistant1 不动
    session_manager.update_message.assert_awaited_once()
    updated = session_manager.update_message.await_args.args[0]
    results = [b for b in updated.content if isinstance(b, ToolResultBlock)]
    assert results[0].output[0].text == "[工具输出已压缩]"


@pytest.mark.asyncio
async def test_fast_compact_keep_turns_covers_all_returns_false():
    """keep_turns 覆盖全部对话轮时无可裁剪工具结果，返回 False。"""
    user0 = UserMsg(name=MsgName.DEFAULT, content="唯一一轮")
    assistant0 = AssistantMsg(
        name=MsgName.DEFAULT,
        content=[
            ToolCallBlock(id="c1", name="read", arguments={}),
            ToolResultBlock(id="c1", name="read", output="output", state="success"),
        ],
    )
    records = [_record(user0, 1), _record(assistant0, 2)]
    session_manager = AsyncMock()
    session_manager.get_context_messages.return_value = records
    emitted: list = []

    async def emit_event(session_id, channel_id, event):
        emitted.append(event)

    manager = CompactManager(session_manager=session_manager, emit_event=emit_event)
    # keep_turns=1 保护这唯一一轮 → 无可裁剪 → False
    changed = await manager.compress_fast(
        "ws::session", "ws", config=SimpleNamespace(), keep_turns=1,
    )
    assert changed is False
    session_manager.update_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_fast_compact_ignores_compact_fast_bubble_in_turn_count():
    """compact_fast 气泡（role=assistant）不被 keep_turns 的 user-turn 计数误当成一轮。

    模拟连续两次 fast 压缩：第一次已生成一条 assistant/compact_fast 气泡插在历史里，
    第二次 keep_turns=1 时应只保护「最近一个真实 user 轮」，气泡不占轮次。
    """
    # 真实轮 0：user0 → assistant0(old 工具结果)
    user0 = UserMsg(name=MsgName.DEFAULT, content="第一轮")
    assistant0 = AssistantMsg(
        name=MsgName.DEFAULT,
        content=[
            ToolCallBlock(id="c-old", name="read", arguments={}),
            ToolResultBlock(id="c-old", name="read", output="old output", state="success"),
        ],
    )
    # 上一次 fast 压缩产生的气泡（assistant，name=compact_fast）
    bubble = AssistantMsg(name=MsgName.COMPACT_FAST, content="已快速压缩：1 个工具输出已裁剪")
    # 真实轮 1：user1 → assistant1(recent 工具结果)
    user1 = UserMsg(name=MsgName.DEFAULT, content="第二轮")
    assistant1 = AssistantMsg(
        name=MsgName.DEFAULT,
        content=[
            ToolCallBlock(id="c-recent", name="read", arguments={}),
            ToolResultBlock(id="c-recent", name="read", output="recent output", state="success"),
        ],
    )
    records = [
        _record(user0, 1), _record(assistant0, 2), _record(bubble, 3),
        _record(user1, 4), _record(assistant1, 5),
    ]
    session_manager = AsyncMock()
    session_manager.get_context_messages.return_value = records
    emitted: list = []

    async def emit_event(session_id, channel_id, event):
        emitted.append(event)

    manager = CompactManager(session_manager=session_manager, emit_event=emit_event)
    # keep_turns=1：气泡不占轮 → 保护 user1 轮（recent 保留），old 被裁
    changed = await manager.compress_fast(
        "ws::session", "ws", config=SimpleNamespace(), keep_turns=1,
    )
    assert changed is True
    # 只裁了 assistant0 的 old，assistant1 的 recent 保留
    session_manager.update_message.assert_awaited_once()
    updated = session_manager.update_message.await_args.args[0]
    results = [b for b in updated.content if isinstance(b, ToolResultBlock)]
    assert results[0].output[0].text == "[工具输出已压缩]"


def test_build_prompt_and_body_estimate():
    text = "[User]: 请修复数据库\n\n[Assistant]: 已完成分析"
    assert _estimate_body_chars(text) > 0
    prompt = _build_prompt(previous_summary="old", context=[text], min_chars=200)
    assert "<conversation>" in prompt[0]
    assert "<previous-summary>" in prompt[1]
    assert "更新锚定摘要" in prompt[-1]


def test_build_prompt_injects_focus_hint():
    """focus_hint 非空时在指令末尾追加强调段。"""
    text = "[User]: 登录模块的实现\n\n[Assistant]: 已完成"
    prompt = _build_prompt(context=[text], min_chars=200, focus_hint="登录模块相关代码")
    assert "【用户强调】" in prompt[-1]
    assert "登录模块相关代码" in prompt[-1]


def test_build_prompt_no_focus_hint_unchanged():
    """无 focus_hint 时指令不含强调段（与旧行为一致）。"""
    text = "[User]: 普通对话\n\n[Assistant]: 好的"
    prompt = _build_prompt(context=[text], min_chars=200)
    assert "【用户强调】" not in prompt[-1]
    prompt_blank = _build_prompt(context=[text], min_chars=200, focus_hint="   ")
    assert "【用户强调】" not in prompt_blank[-1]
