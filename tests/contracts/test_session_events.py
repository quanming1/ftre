"""会话事件表 golden 契约测试（PRD-F41 §4.2 / AC1）。

13 种事件逐一构造合法 data，经 SessionLog.append 产出信封后断言：
- 信封形状 {type, seq, time, message_id, data}；
- data model 校验往返（model_validate(model_dump)）；
- wire 序列化 JSON 纯净（可 json.dumps round-trip）；
- SURFACE / ALL 注册表与事件表一一对应。
"""
from __future__ import annotations

import json

import pytest
from ftre_agent.message import AssistantMsg
from ftre_agent.session import SessionLog
from ftre_agent.session.events import (
    ALL_EVENT_TYPES,
    SURFACE_EVENT_TYPES,
    ApprovalAsked,
    ApprovalAskedData,
    AssistantChunk,
    AssistantChunkData,
    AssistantMessage,
    AssistantMessageData,
    CompactData,
    CompactMessage,
    HintData,
    HintMessage,
    SessionStatus,
    SessionStatusData,
    ToolCallStart,
    ToolCallStartData,
    ToolResult,
    ToolResultData,
    ToolResultStart,
    ToolResultStartData,
    TurnEnd,
    TurnEndData,
    TurnRetry,
    TurnRetryData,
    TurnStart,
    TurnStartData,
    UserMessage,
    UserMessageData,
)


def _assistant_dump() -> dict:
    return AssistantMsg(content="hello").model_dump(mode="json")


# (事件模型, 合法 data, 是否 surface)
EVENT_TABLE = [
    (UserMessage, UserMessageData(content=[{"type": "text", "text": "hi"}], request_id="r1"), True),
    (AssistantMessage, AssistantMessageData(message=_assistant_dump()), True),
    (ToolResult, ToolResultData(tool_call_id="tc1", name="bash", state="success", output=[{"type": "text", "text": "ok"}]), True),
    (HintMessage, HintData(hint="tip", source="skill-extension"), True),
    (CompactMessage, CompactData(mode="summary", summary_text="...", through_message_id="m1"), True),
    (AssistantChunk, AssistantChunkData(kind="text", delta="he", block_id="b1"), False),
    (AssistantChunk, AssistantChunkData(kind="tool_result_text", delta="out", tool_call_id="tc1"), False),
    (ToolCallStart, ToolCallStartData(tool_call_id="tc1", name="bash", arguments={"cmd": "ls"}), False),
    (ToolResultStart, ToolResultStartData(tool_call_id="tc1", name="bash"), False),
    (ApprovalAsked, ApprovalAskedData(tool_call_id="tc1", name="bash", reason="需要确认", rule_id="rule-1"), False),
    (TurnStart, TurnStartData(turn_id="t1", request_id="r1", trigger="user", model="test-model"), False),
    (TurnRetry, TurnRetryData(turn_id="t1", code="llm_error", message="timeout", attempt=1, max_attempts=3), False),
    (TurnEnd, TurnEndData(turn_id="t1", request_id="r1", outcome="completed", usage={"total_tokens": 10}), False),
    (SessionStatus, SessionStatusData(status="blocked", reason="context_overflow"), False),
]


@pytest.mark.parametrize(("model_cls", "data", "is_surface"), EVENT_TABLE)
def test_event_envelope_shape_and_roundtrip(model_cls, data, is_surface):
    log = SessionLog("ws_sess_golden")
    event = model_cls(data=data, message_id="m1" if is_surface or model_cls is AssistantChunk else None)
    stored = log.append(event.type, data.model_dump(mode="json"), message_id=event.message_id)

    # 信封形状
    assert stored["type"] == event.type
    assert set(stored) == {"type", "seq", "time", "message_id", "data"}
    assert isinstance(stored["seq"], int) and stored["seq"] == 0
    assert isinstance(stored["time"], int)
    # data model 往返
    rebuilt = model_cls.model_validate(stored)
    assert rebuilt.data.model_dump(mode="json") == stored["data"]
    # wire 序列化 JSON 纯净
    assert json.loads(json.dumps(stored, ensure_ascii=False)) == stored
    # surface 语义
    assert (stored["type"] in SURFACE_EVENT_TYPES) == is_surface


def test_registry_matches_event_table():
    table_types = {model_cls.model_fields["type"].default for model_cls, _, _ in EVENT_TABLE}
    assert table_types == ALL_EVENT_TYPES
    assert len(table_types) == 13
    assert SURFACE_EVENT_TYPES <= ALL_EVENT_TYPES


def test_unknown_event_type_is_rejected():
    log = SessionLog("ws_sess_golden")
    with pytest.raises(Exception, match="unknown|未知|type"):
        log.append("future/x", {"anything": True})
