"""
compact_handler 模块级算法工具的单测。

只测纯函数，不依赖 db / channel / bus。
"""
from __future__ import annotations

from ftre.agent.compact_handler import (
    DEFAULT_CONSOLIDATION_RATIO,
    DEFAULT_SAFETY_BUFFER,
    _count_user_turns,
    calculate_budget_target,
    get_cursor_index,
    get_previous_summary,
    pick_compaction_boundary,
    raw_archive_chunk,
)


# ─── calculate_budget_target ───────────────────────────────────────────


def test_budget_target_basic():
    # 32K 上下文，max_output=4K → budget = 32000 - 4000 - 1024 = 26976
    # target = 26976 * 0.7 = 18883
    budget, target = calculate_budget_target(32000, 4000)
    assert budget == 32000 - 4000 - DEFAULT_SAFETY_BUFFER
    assert target == int(budget * DEFAULT_CONSOLIDATION_RATIO)


def test_budget_target_max_output_fallback():
    # max_output 缺省 → 退回 cw * 0.2
    budget, target = calculate_budget_target(10000, None)
    assert budget == 10000 - int(10000 * 0.2) - DEFAULT_SAFETY_BUFFER
    assert target > 0


def test_budget_target_invalid_window():
    assert calculate_budget_target(0, 1000) == (0, 0)
    assert calculate_budget_target(-1, 1000) == (0, 0)


def test_budget_target_custom_ratio():
    budget, target = calculate_budget_target(20000, 2000, consolidation_ratio=0.5)
    assert target == int(budget * 0.5)


# ─── get_cursor_index / get_previous_summary ──────────────────────────


def test_cursor_no_compact():
    events = [
        {"type": "USER_INPUT", "data": {"content": "hi"}},
        {"type": "message_complete", "data": {"content": "yes"}},
    ]
    assert get_cursor_index(events) == 0
    assert get_previous_summary(events) is None


def test_cursor_with_one_compact():
    events = [
        {"type": "USER_INPUT", "data": {"content": "a"}},
        {"type": "context_compact", "data": {"summary": "## summary v1"}},
        {"type": "USER_INPUT", "data": {"content": "b"}},
    ]
    # context_compact 在 idx 1，cursor 应指向 idx 2
    assert get_cursor_index(events) == 2
    assert get_previous_summary(events) == "## summary v1"


def test_cursor_with_multiple_compacts_takes_latest():
    events = [
        {"type": "USER_INPUT", "data": {"content": "a"}},
        {"type": "context_compact", "data": {"summary": "v1"}},
        {"type": "USER_INPUT", "data": {"content": "b"}},
        {"type": "context_compact", "data": {"summary": "v2"}},
        {"type": "USER_INPUT", "data": {"content": "c"}},
    ]
    assert get_cursor_index(events) == 4
    assert get_previous_summary(events) == "v2"


# ─── pick_compaction_boundary ─────────────────────────────────────────


def _make_turn(content: str, *, big_text: str = "") -> list[dict]:
    """构造一个简单 user-turn：USER_INPUT + message_complete。"""
    return [
        {"type": "USER_INPUT", "data": {"content": content}},
        {"type": "message_complete", "data": {"content": big_text or "ok"}},
    ]


def test_boundary_returns_none_when_only_one_turn():
    # 只有一个 USER_INPUT —— 不能切（tail 会为空）
    events = _make_turn("only one")
    assert pick_compaction_boundary(events, 0, tokens_to_remove=10) is None


def test_boundary_returns_none_when_no_user_turns_after_cursor():
    events = [
        {"type": "USER_INPUT", "data": {"content": "old"}},
        {"type": "message_complete", "data": {"content": "x"}},
    ]
    # cursor 已经在末尾之后
    assert pick_compaction_boundary(events, 2, tokens_to_remove=10) is None


def test_boundary_picks_first_qualifying_user_turn():
    # 4 轮，每轮文本短；要求移除 1 token 即可 → 应返回第 2 个 USER_INPUT 索引
    events = _make_turn("turn1") + _make_turn("turn2") + _make_turn("turn3") + _make_turn("turn4")
    # turn1 = idx [0,1], turn2 起点 = idx 2
    boundary = pick_compaction_boundary(events, 0, tokens_to_remove=1)
    assert boundary == 2
    assert events[boundary]["type"] == "USER_INPUT"


def test_boundary_falls_back_to_last_safe_when_not_enough():
    # 4 轮短文本，要求移除巨多 token → 扫到尾还不够，返回最后一个合法边界
    # （倒数第 2 个 user-turn 起点，保证 tail 非空）
    events = _make_turn("a") + _make_turn("b") + _make_turn("c") + _make_turn("d")
    # 4 个 USER_INPUT 在 idx [0, 2, 4, 6]；最后合法边界 = idx 4（idx 6 会让 tail 为空）
    boundary = pick_compaction_boundary(events, 0, tokens_to_remove=10**9)
    assert boundary == 4


def test_boundary_respects_cursor_skip_already_compacted():
    # 前 2 轮已经被压过（cursor=4），新的待压区从 idx 4 开始
    events = _make_turn("a") + _make_turn("b") + _make_turn("c") + _make_turn("d")
    boundary = pick_compaction_boundary(events, cursor_idx=4, tokens_to_remove=1)
    # cursor=4 自身是 USER_INPUT，跳过；下一个是 idx 6 → 但 idx 6 是最末 USER_INPUT
    # → 切在那里 tail 为空 → 应返回 None
    assert boundary is None


def test_boundary_lands_on_user_turn_start_preserving_tool_pairs():
    # 关键不变量验证：返回的边界一定是 USER_INPUT，绝不会切在 tool_call 与 tool_result 之间
    events = [
        {"type": "USER_INPUT", "data": {"content": "请帮我跑命令"}},
        {"type": "tool_call", "data": {"id": "c1", "name": "bash", "arguments": {}}},
        {"type": "tool_result", "data": {"id": "c1", "result": "ok"}},
        {"type": "message_complete", "data": {"content": "done"}},
        {"type": "USER_INPUT", "data": {"content": "继续"}},
        {"type": "message_complete", "data": {"content": "好"}},
        {"type": "USER_INPUT", "data": {"content": "再来"}},
        {"type": "message_complete", "data": {"content": "嗯"}},
    ]
    boundary = pick_compaction_boundary(events, 0, tokens_to_remove=1)
    assert boundary is not None
    assert events[boundary]["type"] == "USER_INPUT"


# ─── raw_archive_chunk ────────────────────────────────────────────────


def test_raw_archive_empty():
    assert raw_archive_chunk([]) == ""


def test_raw_archive_preserves_user_full_text():
    # 用户原话必须一字不漏
    chunk = [
        {"type": "USER_INPUT", "data": {"content": "请按文档第 3 节实现压缩算法，注意游标只进不退"}},
        {"type": "message_complete", "data": {"content": "好的"}},
    ]
    out = raw_archive_chunk(chunk)
    assert "请按文档第 3 节实现压缩算法，注意游标只进不退" in out
    assert "## " in out  # 含 markdown 标题


def test_raw_archive_truncates_long_tool_result():
    big = "x" * 2000
    chunk = [
        {"type": "USER_INPUT", "data": {"content": "go"}},
        {"type": "tool_call", "data": {"id": "c1", "name": "bash", "arguments": {}}},
        {"type": "tool_result", "data": {"id": "c1", "result": big}},
    ]
    out = raw_archive_chunk(chunk, tool_result_max=500)
    # tool_result 被截到 500 + 截断标记
    assert "x" * 500 in out
    assert "x" * 600 not in out
    assert "[截断" in out


def test_raw_archive_inlines_previous_summary():
    chunk = [
        {"type": "context_compact", "data": {"summary": "## 旧摘要内容"}},
        {"type": "USER_INPUT", "data": {"content": "新一轮"}},
    ]
    out = raw_archive_chunk(chunk)
    assert "## 旧摘要内容" in out
    assert "之前的历史摘要" in out


def test_raw_archive_previous_summary_param():
    # previous_summary 作为独立参数注入（head 不含旧 compact 事件的场景）
    chunk = [
        {"type": "USER_INPUT", "data": {"content": "新内容"}},
    ]
    out = raw_archive_chunk(chunk, previous_summary="## 上一份摘要正文")
    assert "## 上一份摘要正文" in out
    assert "之前的历史摘要" in out
    assert "新内容" in out


def test_raw_archive_only_previous_summary_no_chunk():
    # chunk 空但有 previous_summary：仍应产出非空
    out = raw_archive_chunk([], previous_summary="## 仅旧摘要")
    assert "## 仅旧摘要" in out


def test_raw_archive_empty_with_no_summary():
    assert raw_archive_chunk([], previous_summary=None) == ""


def test_raw_archive_handles_multimodal_user_content():
    # USER_INPUT 的 content 可能是 list（多模态 v2 协议）
    chunk = [
        {
            "type": "USER_INPUT",
            "data": {"content": [{"type": "text", "data": "看下这张图"}]},
        },
    ]
    out = raw_archive_chunk(chunk)
    assert "看下这张图" in out


# ─── _count_user_turns ────────────────────────────────────────────────


def test_count_user_turns():
    events = _make_turn("a") + _make_turn("b") + _make_turn("c")
    assert _count_user_turns(events) == 3
    assert _count_user_turns([]) == 0
    assert _count_user_turns([{"type": "message_complete", "data": {}}]) == 0
