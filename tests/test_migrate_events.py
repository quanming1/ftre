"""migrate_events 单测：旧事件行 → 新消息行。"""
from __future__ import annotations

import json
import pytest
from ftre.session.migrate_events import _coalesce_session_rows


def _row(id: str, t: str, data: dict, ts: float) -> dict:
    return {"id": id, "session_id": "s1", "type": t, "data": json.dumps(data), "timestamp": ts}


def test_simple_final_reply():
    """一轮最终回复：usage_update + assistant_message_complete(str) + done → 1 assistant + 1 done。"""
    rows = [
        _row("a", "usage_update", {"usage": {"total_tokens": 100}, "event_id": "a"}, 1.0),
        _row("b", "assistant_message_complete", {"content": "hello", "kind": "final", "event_id": "b"}, 1.1),
        _row("c", "done", {"success": True, "reason": "completed", "event_id": "c"}, 1.2),
    ]
    msgs = _coalesce_session_rows(rows)
    assert len(msgs) == 2

    assert msgs[0]["type"] == "assistant_message_complete"
    assert msgs[0]["data"]["content"] == [{"type": "text", "text": "hello", "event_id": "b"}]
    assert msgs[0]["data"]["metadata"]["kind"] == "final"
    assert msgs[0]["data"]["metadata"]["usage"]["total_tokens"] == 100

    assert msgs[1]["type"] == "done"
    assert msgs[1]["data"]["success"] is True


def test_tool_call_round():
    """一轮有工具调用：usage + reasoning + assistant_text + tool_call → 1 assistant(合并)。"""
    rows = [
        _row("a", "usage_update", {"usage": {"total_tokens": 200}, "event_id": "a"}, 1.0),
        _row("b", "reasoning_complete", {"content": "Let me think...", "event_id": "b"}, 1.1),
        _row("c", "assistant_message_complete", {"content": "I'll read it", "kind": "block", "event_id": "c"}, 1.2),
        _row("d", "tool_call", {"id": "tc1", "name": "read", "arguments": {"path": "a.py"}, "event_id": "d"}, 1.3),
        _row("e", "tool_result", {"id": "tc1", "name": "read", "result": "content A", "error": None}, 1.4),
    ]
    msgs = _coalesce_session_rows(rows)
    assert len(msgs) == 2  # 1 assistant + 1 tool_result

    am = msgs[0]
    assert am["type"] == "assistant_message_complete"
    content = am["data"]["content"]
    assert {"type": "thinking", "thinking": "Let me think...", "event_id": "b"} in content
    assert {"type": "text", "text": "I'll read it", "event_id": "c"} in content
    assert {"type": "toolCall", "id": "tc1", "name": "read", "arguments": {"path": "a.py"}, "event_id": "d"} in content
    assert am["data"]["metadata"]["kind"] == "block"
    assert am["data"]["metadata"]["usage"]["total_tokens"] == 200

    assert msgs[1]["type"] == "tool_result"


def test_multi_turn_react():
    """多轮 ReAct：3 轮工具调用 + 最终回复。"""
    rows = [
        _row("u1", "user_message", {"content": "query", "metadata": {"hide": False}}, 0.0),
        # 第 1 轮
        _row("a1", "usage_update", {"usage": {"total_tokens": 100}, "event_id": "a1"}, 1.0),
        _row("c1", "assistant_message_complete", {"content": "let me check", "kind": "block", "event_id": "c1"}, 1.1),
        _row("t1", "tool_call", {"id": "tc1", "name": "bash", "arguments": {}, "event_id": "t1"}, 1.2),
        _row("r1", "tool_result", {"id": "tc1", "name": "bash", "result": "output1", "error": None}, 1.3),
        # 第 2 轮
        _row("a2", "usage_update", {"usage": {"total_tokens": 200}, "event_id": "a2"}, 2.0),
        _row("c2", "assistant_message_complete", {"content": "done", "kind": "final", "event_id": "c2"}, 2.1),
        _row("d2", "done", {"success": True, "reason": "completed", "event_id": "d2"}, 2.2),
    ]
    msgs = _coalesce_session_rows(rows)
    # user + assistant(toolCall) + tool_result + assistant(text) + done = 5
    assert len(msgs) == 5

    assert msgs[0]["type"] == "user_message"
    assert msgs[1]["type"] == "assistant_message_complete"
    assert msgs[1]["data"]["metadata"]["kind"] == "block"
    assert any(b.get("type") == "toolCall" for b in msgs[1]["data"]["content"])
    assert msgs[2]["type"] == "tool_result"
    assert msgs[3]["type"] == "assistant_message_complete"
    assert msgs[3]["data"]["metadata"]["kind"] == "final"
    assert msgs[4]["type"] == "done"


def test_done_strips_usage():
    """done 旧格式可能有 usage，新格式应去掉。"""
    rows = [
        _row("a", "done", {"success": True, "reason": "completed", "usage": {"total_tokens": 50}}, 1.0),
    ]
    msgs = _coalesce_session_rows(rows)
    assert len(msgs) == 1
    assert msgs[0]["type"] == "done"
    assert "usage" not in msgs[0]["data"]


def test_error_embedded():
    """error 事件嵌入 assistant metadata。"""
    rows = [
        _row("a", "assistant_message_complete", {"content": "partial", "kind": "block", "event_id": "a"}, 1.0),
        _row("b", "error", {"message": "LLM timeout", "code": "timeout", "event_id": "b"}, 1.1),
        _row("c", "done", {"success": False, "reason": "error", "event_id": "c"}, 1.2),
    ]
    msgs = _coalesce_session_rows(rows)
    assert len(msgs) == 2  # 1 assistant(error) + 1 done

    assert msgs[0]["type"] == "assistant_message_complete"
    assert msgs[0]["data"]["metadata"]["stopReason"] == "error"
    assert msgs[0]["data"]["metadata"]["error"]["message"] == "LLM timeout"
    assert msgs[0]["data"]["metadata"]["error"]["code"] == "timeout"


def test_new_format_passthrough():
    """新格式 assistant_message_complete（content 是 list）直接透传。"""
    rows = [
        _row("a", "assistant_message_complete", {
            "content": [{"type": "text", "text": "already new", "event_id": "a"}],
            "metadata": {"kind": "final"},
            "event_id": "a",
        }, 1.0),
    ]
    msgs = _coalesce_session_rows(rows)
    assert len(msgs) == 1
    assert msgs[0]["type"] == "assistant_message_complete"
    assert msgs[0]["data"]["content"] == [{"type": "text", "text": "already new", "event_id": "a"}]


def test_context_compact_passthrough():
    """context_compact 原样透传。"""
    rows = [
        _row("a", "user_message", {"content": "old", "metadata": {"hide": False}}, 1.0),
        _row("b", "context_compact", {"summary": "## summary", "enabled": True}, 1.1),
        _row("c", "user_message", {"content": "new", "metadata": {"hide": False}}, 1.2),
    ]
    msgs = _coalesce_session_rows(rows)
    assert len(msgs) == 3
    assert msgs[1]["type"] == "context_compact"
    assert msgs[1]["data"]["summary"] == "## summary"


def test_empty_text_omitted():
    """空文本的 assistant_message_complete 不产出空消息。"""
    rows = [
        _row("a", "usage_update", {"usage": {"total_tokens": 10}, "event_id": "a"}, 1.0),
        _row("b", "done", {"success": True, "reason": "completed"}, 1.1),
    ]
    msgs = _coalesce_session_rows(rows)
    # usage 没有 content → 不产出 assistant message
    assert len(msgs) == 1
    assert msgs[0]["type"] == "done"
