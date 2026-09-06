"""SessionLog / derive_messages 单元测试（PRD-F43 AC1/AC5 基础）。"""
from __future__ import annotations

import pytest
from ftre_agent.message import MsgName
from ftre_agent.session import SessionLog, derive_context_messages, derive_messages


def _mk_log() -> SessionLog:
    return SessionLog("ws_sess_test")


class TestSessionLogAppend:
    def test_seq_strictly_increasing(self):
        log = _mk_log()
        for i in range(5):
            event = log.append("turn/start", {"turn_id": f"t{i}", "trigger": "user"})
            assert event["seq"] == i
        assert log.last_seq == 4

    def test_envelope_shape(self):
        log = _mk_log()
        event = log.append(
            "assistant/chunk",
            {"kind": "text", "delta": "hi"},
            message_id="m1",
        )
        assert set(event) == {"type", "seq", "time", "message_id", "data"}
        assert event["type"] == "assistant/chunk"
        assert event["message_id"] == "m1"
        assert event["data"] == {"kind": "text", "delta": "hi"}
        assert event["time"] > 0

    def test_rejects_non_json_values(self):
        log = _mk_log()
        with pytest.raises(TypeError):
            log.append("turn/start", {"turn_id": {"bad": object()}})

    def test_rejects_unknown_type(self):
        log = _mk_log()
        with pytest.raises(ValueError, match="未知事件类型"):
            log.append("future/thing", {"x": 1})

    def test_reentrancy_forbidden(self):
        log = _mk_log()
        holder = {}

        def bad_subscriber(event):
            holder["nested"] = log.append("turn/retry", {
                "turn_id": "t", "code": "x", "message": "y",
                "attempt": 1, "max_attempts": 2,
            })

        log.subscribe(bad_subscriber)
        log.append("turn/start", {"turn_id": "t", "trigger": "user"})
        # 嵌套 append 被订阅者异常吞掉（containment），主事件仍提交成功
        assert log.last_seq == 0

    def test_subscriber_failure_isolated(self):
        log = _mk_log()
        seen = []

        def boom(_event):
            raise RuntimeError("observer down")

        def ok(event):
            seen.append(event["seq"])

        log.subscribe(boom)
        log.subscribe(ok)
        log.append("turn/start", {"turn_id": "t", "trigger": "user"})
        assert seen == [0]

    def test_append_copies_input_data(self):
        log = _mk_log()
        data = {"turn_id": "t", "trigger": "user"}
        log.append("turn/start", data)
        data["trigger"] = "mutated"
        assert log.events[0]["data"]["trigger"] == "user"

    def test_user_message_idempotent(self):
        log = _mk_log()
        first = log.append_user_message(
            request_id="req_1", content=[{"type": "text", "text": "hi"}]
        )
        assert first is not None
        assert first["message_id"].startswith("user_")
        second = log.append_user_message(
            request_id="req_1", content=[{"type": "text", "text": "hi"}]
        )
        assert second is None
        assert log.last_seq == 0

    def test_user_message_fingerprint_conflict(self):
        log = _mk_log()
        log.append_user_message(request_id="req_1", content=[{"type": "text", "text": "a"}])
        with pytest.raises(ValueError, match="已绑定不同内容"):
            log.append_user_message(
                request_id="req_1", content=[{"type": "text", "text": "b"}]
            )

    def test_request_state(self):
        log = _mk_log()
        log.append("turn/start", {"turn_id": "t1", "request_id": "r1", "trigger": "user"})
        assert log.request_state("r1") is None
        log.append("turn/end", {
            "turn_id": "t1", "request_id": "r1",
            "outcome": "completed", "reason": "completed",
        })
        assert log.request_state("r1") == "completed"
        log.append("turn/end", {
            "turn_id": "t2", "request_id": "r2",
            "outcome": "error", "reason": "error",
        })
        assert log.request_state("r2") == "failed"


class TestSessionLogLoad:
    def test_load_rebuilds_and_validates_seq(self):
        log = _mk_log()
        events = [
            {"type": "turn/start", "seq": 0, "time": 1, "message_id": None,
             "data": {"turn_id": "t", "trigger": "user"}},
        ]
        log.load(events)
        assert log.last_seq == 0
        log.append("turn/end", {
            "turn_id": "t", "outcome": "completed", "reason": "completed",
        })
        assert log.events[-1]["seq"] == 1

    def test_load_rejects_seq_gap(self):
        log = _mk_log()
        with pytest.raises(ValueError, match="seq 不连续"):
            log.load([
                {"type": "turn/start", "seq": 1, "time": 1, "message_id": None,
                 "data": {"turn_id": "t", "trigger": "user"}},
            ])

    def test_load_rejects_unknown_type(self):
        log = _mk_log()
        with pytest.raises(ValueError, match="未知事件类型"):
            log.load([
                {"type": "future/x", "seq": 0, "time": 1, "message_id": None, "data": {}},
            ])

    def test_load_rebuilds_user_request_index(self):
        log = _mk_log()
        log.load([
            {"type": "user/message", "seq": 0, "time": 1, "message_id": "user_1",
             "data": {"content": [{"type": "text", "text": "hi"}],
                      "metadata": {}, "request_id": "req_9"}},
        ])
        assert log.has_user_request("req_9")
        assert log.append_user_message(
            request_id="req_9", content=[{"type": "text", "text": "hi"}]
        ) is None


def _full_flow_events() -> list[dict]:
    """一轮完整对话的事件序列（S1 骨架），golden 契约的基础 fixture。"""
    log = SessionLog("ws_sess_golden")
    log.append_user_message(
        request_id="req_a", content=[{"type": "text", "text": "你好"}],
        metadata={"agent_id": "default"},
    )
    log.append("turn/start", {
        "turn_id": "turn_1", "request_id": "req_a",
        "trigger": "user", "agent_id": "default", "model": "deepseek-chat",
    }, )
    log.append("assistant/chunk", {"kind": "text", "delta": "你"}, message_id="m1")
    log.append("assistant/chunk", {"kind": "text", "delta": "好！"}, message_id="m1")
    log.append("tool/call-start", {
        "tool_call_id": "tc_1", "name": "bash", "arguments": {"command": "dir"},
    }, message_id="m1")
    log.append("tool/result-start", {"tool_call_id": "tc_1", "name": "bash"})
    log.append("assistant/chunk", {
        "kind": "tool_result_text", "delta": "src\n", "tool_call_id": "tc_1",
    }, message_id="m1")
    log.append("tool/result", {
        "tool_call_id": "tc_1", "name": "bash",
        "output": [{"type": "text", "text": "src\n"}],
        "state": "success", "metadata": {"exit_code": 0},
    }, message_id="m1")
    from ftre_agent.message import AssistantMsg

    final = AssistantMsg(
        id="m1",
        content=[
            {"type": "text", "text": "你好！", "id": "blk_t1"},
            {"type": "tool_call", "id": "tc_1", "name": "bash",
             "arguments": {"command": "dir"}, "state": "finished"},
            {"type": "tool_result", "id": "tc_1", "name": "bash",
             "output": [{"type": "text", "text": "src\n", "id": "b2"}],
             "state": "success", "metadata": {"exit_code": 0}},
        ],
        metadata={"model": "deepseek-chat"},
    )
    log.append("assistant/message", {"message": final.model_dump(mode="json")}, message_id="m1")
    log.append("turn/end", {
        "turn_id": "turn_1", "request_id": "req_a",
        "outcome": "completed", "reason": "completed",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "iterations": 2,
    }, message_id="m1")
    return list(log.events)


class TestDerive:
    def test_full_flow_golden(self):
        events = _full_flow_events()
        messages = derive_messages(events)
        assert [m.role for m in messages] == ["user", "assistant"]
        user, assistant = messages
        assert user.id.startswith("user_")
        assert user.get_text_content() == "你好"
        assert assistant.id == "m1"
        assert assistant.metadata["model"] == "deepseek-chat"
        types = [b.type for b in assistant.content]
        assert types == ["text", "tool_call", "tool_result"]
        assert assistant.content[1].state == "finished"
        assert assistant.finished_reason == "completed"
        assert assistant.token is not None
        assert assistant.token.usage.total_tokens == 15

    def test_inflight_chunks_are_visible_before_whole_value_snapshot(self):
        log = SessionLog("ws_sess_inflight")
        log.append_user_message(
            request_id="req_inflight",
            content=[{"type": "text", "text": "继续"}],
        )
        log.append(
            "assistant/chunk",
            {"kind": "text", "delta": "正在"},
            message_id="reply_inflight",
        )
        log.append(
            "assistant/chunk",
            {"kind": "text", "delta": "生成"},
            message_id="reply_inflight",
        )
        log.append(
            "assistant/chunk",
            {"kind": "thinking", "delta": "先检查"},
            message_id="reply_inflight",
        )
        log.append(
            "tool/call-start",
            {"tool_call_id": "tc_inflight", "name": "read", "arguments": {}},
            message_id="reply_inflight",
        )
        log.append(
            "tool/result-start",
            {"tool_call_id": "tc_inflight", "name": "read"},
        )
        log.append(
            "assistant/chunk",
            {
                "kind": "tool_result_text",
                "delta": "第一行\n",
                "tool_call_id": "tc_inflight",
            },
            message_id="reply_inflight",
        )
        log.append(
            "assistant/chunk",
            {
                "kind": "tool_result_text",
                "delta": "第二行",
                "tool_call_id": "tc_inflight",
            },
            message_id="reply_inflight",
        )

        user, assistant = derive_messages(list(log.events))
        assert user.get_text_content() == "继续"
        text = next(block for block in assistant.content if block.type == "text")
        thinking = next(block for block in assistant.content if block.type == "thinking")
        result = next(block for block in assistant.content if block.type == "tool_result")
        assert text.text == "正在生成"
        assert thinking.thinking == "先检查"
        assert result.state == "running"
        assert result.name == "read"
        assert result.output[0].text == "第一行\n第二行"
        assert assistant.finished_at is None

    def test_idempotent_replay(self):
        events = _full_flow_events()
        once = derive_messages(events)
        twice = derive_messages(events + events)
        assert [m.id for m in once] == [m.id for m in twice]

    def test_user_message_seals_previous_assistant(self):
        log = SessionLog("s")
        log.append("assistant/message", {"message": {
            "name": "default", "role": "assistant", "id": "m1", "content": [],
            "metadata": {}, "created_at": "2026-01-01T00:00:00+00:00",
        }}, message_id="m1")
        log.append_user_message(request_id="r2", content=[{"type": "text", "text": "next"}])
        messages = derive_messages(list(log.events))
        assert messages[0].finished_reason == "completed"

    def test_context_anchor_trimming(self):
        log = SessionLog("s")
        log.append_user_message(request_id="r1", content=[{"type": "text", "text": "旧轮"}])
        log.append("compact/message", {
            "mode": "summary", "summary_text": "摘要内容",
            "through_message_id": "user_r1", "trigger": "auto",
        }, message_id="c1")
        log.append_user_message(request_id="r2", content=[{"type": "text", "text": "新轮"}])
        context = derive_context_messages(list(log.events))
        assert [m.get_text_content() for m in context if m.role == "user"] == ["摘要内容", "新轮"]
        full = derive_messages(list(log.events))
        assert len(full) == 3

    def test_fast_compact_elides_tool_results(self):
        log = SessionLog("s")
        log.append_user_message(request_id="r1", content=[{"type": "text", "text": "go"}])
        log.append("tool/call-start", {
            "tool_call_id": "tc_a", "name": "read", "arguments": {"path": "x"},
        }, message_id="m1")
        log.append("tool/result", {
            "tool_call_id": "tc_a", "name": "read",
            "output": [{"type": "text", "text": "很长的文件内容"}],
            "state": "success", "metadata": {},
        }, message_id="m1")
        log.append("compact/message", {
            "mode": "fast", "tool_results": 1,
            "tokens_before": 1000, "tokens_after": 200,
        }, message_id="cf1")
        context = derive_context_messages(list(log.events))
        assistant = next(m for m in context if m.role == "assistant")
        result_block = next(b for b in assistant.content if b.type == "tool_result")
        assert "已压缩裁剪" in result_block.output[0].text
        full = derive_messages(list(log.events))
        assistant_full = next(m for m in full if m.role == "assistant")
        result_full = next(b for b in assistant_full.content if b.type == "tool_result")
        assert result_full.output[0].text == "很长的文件内容"

    def test_compact_bubble_names(self):
        log = SessionLog("s")
        log.append("compact/message", {
            "mode": "fast", "tool_results": 2,
            "tokens_before": 100, "tokens_after": 50,
        }, message_id="cf1")
        messages = derive_messages(list(log.events))
        assert messages[0].name == MsgName.COMPACT_FAST
        assert messages[0].role == "assistant"

    def test_user_blocks_are_deterministic_and_unknown_parts_do_not_break_fold(self):
        event = {
            "type": "user/message",
            "seq": 7,
            "time": 1_700_000_000_000,
            "message_id": "user-7",
            "data": {
                "request_id": "r7",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "skill", "data": "review-code"},
                    {"type": "image_file", "path": "missing.png"},
                ],
            },
        }
        first = derive_messages([event])[0]
        second = derive_messages([event])[0]
        assert [block.id for block in first.content] == [block.id for block in second.content]
        assert [block.created_at for block in first.content] == [
            block.created_at for block in second.content
        ]
        assert "review-code" in first.get_text_content()

    def test_fast_compact_uses_exact_tool_result_ids(self):
        events = [
            {"type": "tool/call-start", "seq": 0, "time": 1, "message_id": "m1",
             "data": {"tool_call_id": "old", "name": "read", "arguments": {}}},
            {"type": "tool/result", "seq": 1, "time": 2, "message_id": "m1",
             "data": {"tool_call_id": "old", "name": "read", "output": [{"type": "text", "text": "old"}], "state": "success"}},
            {"type": "compact/message", "seq": 2, "time": 3, "message_id": "c1",
             "data": {"mode": "fast", "tool_results": 1, "tool_result_ids": ["old"]}},
            {"type": "tool/call-start", "seq": 3, "time": 4, "message_id": "m2",
             "data": {"tool_call_id": "new", "name": "read", "arguments": {}}},
            {"type": "tool/result", "seq": 4, "time": 5, "message_id": "m2",
             "data": {"tool_call_id": "new", "name": "read", "output": [{"type": "text", "text": "new"}], "state": "success"}},
        ]
        context = derive_context_messages(events)
        by_id = {
            block.id: block
            for message in context
            for block in message.content
            if block.type == "tool_result"
        }
        assert by_id["old"].output[0].text.startswith("[已压缩裁剪")
        assert by_id["new"].output[0].text == "new"


class TestTail:
    def test_tail_paging(self):
        log = _mk_log()
        for i in range(10):
            log.append("turn/retry", {
                "turn_id": "t", "code": "x", "message": "y",
                "attempt": i, "max_attempts": 10,
            })
        page, has_more = log.tail(-1, 4)
        assert [e["seq"] for e in page] == [0, 1, 2, 3]
        assert has_more
        page, has_more = log.tail(8, 4)
        assert [e["seq"] for e in page] == [9]
        assert not has_more
