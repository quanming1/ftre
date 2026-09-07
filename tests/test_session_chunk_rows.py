"""流式 chunk 仅在内存聚合，Snapshot 不产生 chunk storage row。"""
from __future__ import annotations

import json

import pytest

from ftre.services.session.persistence.snapshot import read_legacy_events
from ftre.services.session.service import SessionService


@pytest.mark.asyncio
async def test_chunks_are_folded_into_msg_not_written_as_rows(tmp_path):
    manager = SessionService(
        sessions_dir=str(tmp_path / "sessions"), snapshot_interval_ms=10
    )
    await manager.init()
    sid = await manager.create_session("ws")
    for index in range(100):
        await manager.append_event(
            sid,
            "assistant/chunk",
            {"kind": "text", "delta": str(index), "block_id": "b1"},
            message_id="m1",
        )
    await manager.flush_log(sid)
    payload = json.loads(
        (manager.session_dir(sid) / "session.json").read_text(encoding="utf-8")
    )
    assert payload["messages"][0]["content"][0]["text"] == "".join(
        str(index) for index in range(100)
    )
    text = json.dumps(payload, ensure_ascii=False)
    assert "thinking-chunks" not in text
    assert "text-chunks" not in text
    await manager.close()


def test_legacy_packed_rows_are_only_read_during_migration(tmp_path):
    directory = tmp_path / "legacy"
    directory.mkdir()
    row = {
        "type": "text-chunks",
        "seq0": 0,
        "time0": 1000,
        "message_id": "m1",
        "data": {
            "kind": "text",
            "block_id": "b1",
            "tool_call_id": None,
            "dt": [1, 1],
            "deltas": ["a", "b", "c"],
        },
    }
    (directory / "session.jsonl").write_text(
        json.dumps({"v": 1, "format": "ftre-session-log"}) + "\n" + json.dumps(row) + "\n",
        encoding="utf-8",
    )
    events = read_legacy_events(directory)
    assert [event["data"]["delta"] for event in events] == ["a", "b", "c"]
