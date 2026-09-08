"""SessionMetaFile Snapshot Schema 测试（PRD-F44）。

验收标准：
- 合法 Snapshot 可 round-trip（包含 messages / seq）；
- 未知 schema_version 明确报不支持；
- schema_version=1 输入直接拒绝；schema v2 仅作为迁移输入接受。
"""
import pytest
from pydantic import ValidationError

from ftre.services.session.entity.state import (
    SessionMetaFile,
    SessionState,
    UnsupportedSessionMetaVersion,
    parse_session_meta,
    parse_session_meta_json,
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


def test_minimal_meta_round_trip():
    state = SessionMetaFile(session=_session())  # type: ignore[arg-type]
    payload = state.model_dump(mode="json")
    assert set(payload) == {
        "schema_version", "session", "metadata", "seq",
        "messages", "requests", "extensions",
    }
    assert payload["schema_version"] == 5
    assert payload["metadata"] == {}
    restored = parse_session_meta(payload)
    assert restored.session.id == "ws_sess_abc123"
    assert restored.session.last_user_text == ""


def test_meta_json_round_trip():
    state = SessionMetaFile(
        session=_session(last_user_text="最近一条用户消息"),
        metadata={"plan": {"steps": []}},
    )
    payload = state.model_dump_json()
    restored = parse_session_meta_json(payload)
    assert restored.session.last_user_text == "最近一条用户消息"
    assert restored.metadata["plan"] == {"steps": []}


def test_unknown_schema_version_rejected():
    with pytest.raises(UnsupportedSessionMetaVersion):
        parse_session_meta({
            "schema_version": 99,
            "session": _session(),
            "metadata": {},
        })


def test_schema_v1_with_messages_rejected():
    """schema_version=1 且含 messages 的输入直接拒绝（旧格式不解析）。"""
    with pytest.raises(UnsupportedSessionMetaVersion):
        parse_session_meta({
            "schema_version": 1,
            "session": _session(),
            "messages": [],
            "metadata": {},
        })


def test_unknown_fields_are_preserved_for_extensions():
    restored = parse_session_meta({
        "schema_version": 4,
        "session": _session(),
        "metadata": {},
        "cursor": 12,
        "unknown_field": True,
    })
    assert restored.model_dump(mode="json")["unknown_field"] is True
    assert restored.seq == 12


def test_session_state_requires_channel_and_timestamps():
    with pytest.raises(ValidationError):
        SessionState(id="x", created_at="2026-01-01T00:00:00+00:00")
