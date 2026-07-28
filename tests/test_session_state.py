"""AgentStateFile Schema 测试（设计文档 §7 / §18.1）。

验收标准：
- 合法 AgentState 可 round-trip；
- 非法 Msg、重复 ID、悬空 summary cursor 被拒绝；
- 未知 schema_version 明确报不支持。
"""
import pytest
from ftre_agent_core.message import AssistantMsg, SystemMsg, UserMsg
from pydantic import ValidationError

from ftre.session.state import (
    AgentStateFile,
    SummaryState,
    UnsupportedAgentStateVersion,
    parse_agent_state,
    parse_agent_state_json,
)


def _session(**overrides) -> dict:
    base = {
        "id": "ws_sess_abc123",
        "agent_id": "default",
        "channel_id": "ws",
        "title": "测试",
        "workspace": "E:\\ftre",
        "created_at": "2026-07-27T18:00:00+08:00",
        "updated_at": "2026-07-27T21:00:00+08:00",
    }
    base.update(overrides)
    return base


def _user(msg_id: str, text: str = "hi") -> dict:
    return UserMsg(name="default", content=text, id=msg_id).model_dump(mode="json")


def _summary_msg(text: str = "摘要") -> SystemMsg:
    return SystemMsg(
        name="context_compact",
        content=text,
        metadata={"context_compact": {"mode": "summary"}},
    )


def test_minimal_state_round_trip():
    state = AgentStateFile(session=_session()  # type: ignore[arg-type]
                           )
    payload = state.model_dump(mode="json")
    assert set(payload) == {
        "schema_version",
        "session",
        "messages",
        "summary",
        "metadata",
    }
    assert payload["schema_version"] == 1
    assert payload["messages"] == []
    assert payload["summary"] is None
    assert payload["metadata"] == {}

    loaded = parse_agent_state(payload)
    assert loaded == state
    # JSON 文本 round-trip
    assert parse_agent_state_json(state.model_dump_json()) == state


def test_full_state_round_trip():
    user = _user("msg_u1")
    assistant = AssistantMsg(
        name="default", content="回答", id="reply_1"
    ).model_dump(mode="json")
    summary = SummaryState(
        message=_summary_msg("截至 msg_u1 的滚动摘要"),
        through_message_id="msg_u1",
    )
    state = AgentStateFile(
        session=_session(),  # type: ignore[arg-type]
        messages=[user, assistant],  # type: ignore[list-item]
        summary=summary,
        metadata={"plan": None, "external": None},
    )
    loaded = parse_agent_state_json(state.model_dump_json())
    assert loaded == state
    assert loaded.summary is not None
    assert loaded.summary.through_message_id == "msg_u1"
    assert loaded.summary.message.role == "system"


def test_unknown_root_field_rejected():
    payload = AgentStateFile(session=_session()).model_dump(mode="json")  # type: ignore[arg-type]
    payload["unexpected"] = 1
    with pytest.raises(ValidationError):
        parse_agent_state(payload)


def test_unknown_schema_version_rejected():
    payload = AgentStateFile(session=_session()).model_dump(mode="json")  # type: ignore[arg-type]
    payload["schema_version"] = 2
    with pytest.raises(UnsupportedAgentStateVersion):
        parse_agent_state(payload)

    payload["schema_version"] = 0
    with pytest.raises(UnsupportedAgentStateVersion):
        parse_agent_state(payload)

    payload["schema_version"] = None
    with pytest.raises(UnsupportedAgentStateVersion):
        parse_agent_state(payload)


def test_duplicate_msg_id_rejected():
    with pytest.raises(ValidationError, match="duplicate Msg.id"):
        AgentStateFile(
            session=_session(),  # type: ignore[arg-type]
            messages=[_user("dup"), _user("dup", text="again")],  # type: ignore[list-item]
        )


def test_summary_must_be_system_msg():
    with pytest.raises(ValidationError, match="SystemMsg"):
        SummaryState(
            message=UserMsg(name="default", content="not system"),
            through_message_id="msg_u1",
        )


def test_summary_cursor_must_reference_real_message():
    with pytest.raises(ValidationError, match="does not reference"):
        AgentStateFile(
            session=_session(),  # type: ignore[arg-type]
            messages=[_user("msg_u1")],  # type: ignore[list-item]
            summary=SummaryState(
                message=_summary_msg(), through_message_id="msg_missing"
            ),
        )


def test_event_shape_in_messages_rejected():
    event_like = {
        "id": "evt_1",
        "name": "assistant",
        "role": "assistant",
        "type": "TEXT_BLOCK_DELTA",
        "data": {"delta": "x"},
        "content": [],
    }
    with pytest.raises(ValidationError):
        AgentStateFile(
            session=_session(),  # type: ignore[arg-type]
            messages=[event_like],  # type: ignore[list-item]
        )


def test_json_schema_generable():
    schema = AgentStateFile.model_json_schema()
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {
        "schema_version",
        "session",
        "messages",
        "summary",
        "metadata",
    }
