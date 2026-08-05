"""AgentStateFile Schema 测试（新协议：无 summary 字段）。

验收标准：
- 合法 AgentState 可 round-trip；
- 非法 Msg、重复 ID 被拒绝；
- 未知 schema_version 明确报不支持；
- 旧 state.json 残留 summary 字段可被剥离加载。
"""
import pytest
from ftre_agent_core.message import AssistantMsg, MsgName, UserMsg
from pydantic import ValidationError

from ftre.session.entity.state import (
    AgentStateFile,
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
    return UserMsg(name=MsgName.DEFAULT, content=text, id=msg_id).model_dump(mode="json")


def _compact_msg(msg_id: str = "compact_1", text: str = "摘要") -> dict:
    return UserMsg(
        name=MsgName.COMPACT,
        content=text,
        id=msg_id,
        metadata={"hide": True, "context_compact": {"mode": "summary"}},
    ).model_dump(mode="json")


def test_minimal_state_round_trip():
    state = AgentStateFile(session=_session())  # type: ignore[arg-type]
    payload = state.model_dump(mode="json")
    assert set(payload) == {
        "schema_version",
        "session",
        "messages",
        "metadata",
    }
    assert payload["schema_version"] == 1
    assert payload["messages"] == []
    assert payload["metadata"] == {}

    loaded = parse_agent_state(payload)
    assert loaded == state
    assert parse_agent_state_json(state.model_dump_json()) == state


def test_full_state_with_compact_msg_round_trip():
    user = _user("msg_u1")
    assistant = AssistantMsg(
        name=MsgName.DEFAULT, content="回答", id="reply_1"
    ).model_dump(mode="json")
    compact = _compact_msg("compact_1", "截至 msg_u1 的滚动摘要")
    state = AgentStateFile(
        session=_session(),  # type: ignore[arg-type]
        messages=[user, assistant, compact],  # type: ignore[list-item]
        metadata={"plan": None, "external": None},
    )
    loaded = parse_agent_state_json(state.model_dump_json())
    assert loaded == state
    compact_msgs = [m for m in loaded.messages if m.name == MsgName.COMPACT]
    assert len(compact_msgs) == 1
    assert compact_msgs[0].get_text_content() == "截至 msg_u1 的滚动摘要"


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


def test_legacy_summary_field_stripped_on_load():
    """旧 state.json 残留 summary 字段可被剥离，正常加载。"""
    payload = AgentStateFile(
        session=_session(),  # type: ignore[arg-type]
        messages=[_user("msg_u1")],  # type: ignore[list-item]
    ).model_dump(mode="json")
    payload["summary"] = {"message": _compact_msg(), "through_message_id": "msg_u1"}
    loaded = parse_agent_state(payload)
    assert not hasattr(loaded, "summary") or "summary" not in loaded.model_fields
    assert len(loaded.messages) == 1


def test_event_shape_in_messages_rejected():
    event_like = {
        "id": "evt_1",
        "name": "default",
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
        "metadata",
    }
