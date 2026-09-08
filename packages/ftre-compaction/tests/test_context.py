from __future__ import annotations

from ftre_agent.session import SessionLog, derive_messages
from ftre_compaction.context import (
    TRIMMED_TOOL_RESULT_PLACEHOLDER,
    build_context_view,
)


def test_summary_context_is_owned_by_compaction_view_builder():
    log = SessionLog("s")
    log.append_user_message(request_id="r1", content=[{"type": "text", "text": "旧轮"}])
    log.append(
        "compact/message",
        {
            "mode": "summary",
            "summary_text": "摘要内容",
            "through_message_id": "user_r1",
            "trigger": "auto",
        },
        message_id="c1",
    )
    log.append_user_message(request_id="r2", content=[{"type": "text", "text": "新轮"}])

    context = build_context_view(derive_messages(list(log.events)))

    assert [message.get_text_content() for message in context if message.role == "user"] == [
        "摘要内容",
        "新轮",
    ]
    assert len(derive_messages(list(log.events))) == 3


def test_fast_context_replaces_only_marked_tool_results_on_a_copy():
    log = SessionLog("s")
    log.append_user_message(request_id="r1", content=[{"type": "text", "text": "go"}])
    log.append(
        "tool/call-start",
        {"tool_call_id": "tc_a", "name": "read", "arguments": {"path": "x"}},
        message_id="m1",
    )
    log.append(
        "tool/result",
        {
            "tool_call_id": "tc_a",
            "name": "read",
            "output": [{"type": "text", "text": "很长的文件内容"}],
            "state": "success",
            "metadata": {},
        },
        message_id="m1",
    )
    log.append(
        "compact/message",
        {"mode": "fast", "tool_results": 1, "tokens_before": 1000, "tokens_after": 200},
        message_id="cf1",
    )

    full = derive_messages(list(log.events))
    context = build_context_view(full)
    assistant = next(message for message in context if message.role == "assistant")
    result_block = next(block for block in assistant.content if block.type == "tool_result")
    assert result_block.output[0].text == TRIMMED_TOOL_RESULT_PLACEHOLDER

    assistant_full = next(message for message in full if message.role == "assistant")
    result_full = next(block for block in assistant_full.content if block.type == "tool_result")
    assert result_full.output[0].text == "很长的文件内容"


def test_fast_context_honors_exact_tool_result_ids():
    log = SessionLog("s")
    log.append(
        "tool/call-start",
        {"tool_call_id": "old", "name": "read", "arguments": {}},
        message_id="m1",
    )
    log.append(
        "tool/result",
        {
            "tool_call_id": "old",
            "name": "read",
            "output": [{"type": "text", "text": "old"}],
            "state": "success",
        },
        message_id="m1",
    )
    log.append(
        "compact/message",
        {"mode": "fast", "tool_results": 1, "tool_result_ids": ["old"]},
        message_id="c1",
    )
    log.append(
        "tool/call-start",
        {"tool_call_id": "new", "name": "read", "arguments": {}},
        message_id="m2",
    )
    log.append(
        "tool/result",
        {
            "tool_call_id": "new",
            "name": "read",
            "output": [{"type": "text", "text": "new"}],
            "state": "success",
        },
        message_id="m2",
    )

    context = build_context_view(derive_messages(list(log.events)))
    by_id = {
        block.id: block
        for message in context
        for block in message.content
        if block.type == "tool_result"
    }
    assert by_id["old"].output[0].text == TRIMMED_TOOL_RESULT_PLACEHOLDER
    assert by_id["new"].output[0].text == "new"
