"""Session Snapshot + live Event 集成测试（PRD-F44）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from ftre_agent.session import SessionLog

from ftre.services.session.persistence.snapshot import SnapshotCoordinator
from ftre.services.session.service import SessionService


def _mk_log(sid: str = "ws_sess_t") -> SessionLog:
    return SessionLog(sid)


@pytest.mark.asyncio
async def test_snapshot_flush_keeps_one_file_and_no_chunk_rows(tmp_path: Path):
    manager = SessionService(
        str(tmp_path / "sessions.db"),
        sessions_dir=str(tmp_path / "sessions"),
        snapshot_interval_ms=20,
    )
    await manager.init()
    sid = await manager.create_session("ws")
    await manager.append_user_message_if_absent(
        sid, request_id="r1", content=[{"type": "text", "text": "hello"}]
    )
    await manager.append_event(
        sid,
        "assistant/chunk",
        {"kind": "thinking", "delta": "draft", "block_id": "b1"},
        message_id="a1",
    )
    await manager.flush_log(sid)
    directory = manager.session_dir(sid)
    assert (directory / "session.json").exists()
    assert not (directory / "session.jsonl").exists()
    payload = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 5
    assert payload["messages"][1]["content"][0]["thinking"] == "draft"
    assert "thinking-chunks" not in (directory / "session.json").read_text(encoding="utf-8")
    await manager.close()


@pytest.mark.asyncio
async def test_snapshot_coordinator_failure_is_observable_and_retries():
    calls = 0

    async def snapshotter(_session_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk unavailable")

    coordinator = SnapshotCoordinator(snapshotter, interval_ms=10)
    coordinator.mark_dirty("s", immediate=True)
    with pytest.raises(OSError, match="disk unavailable"):
        await coordinator.flush("s")
    await coordinator.flush("s")
    assert calls >= 2
    await coordinator.close()


@pytest.mark.asyncio
async def test_legacy_jsonl_is_migrated_to_snapshot_once(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    manager = SessionService(str(tmp_path / "sessions.db"), sessions_dir=str(sessions_dir))
    await manager.init()
    sid = await manager.create_session("ws")
    legacy = manager.session_dir(sid) / "session.jsonl"
    events = [
        {"type": "turn/start", "seq": 0, "time": 1, "message_id": None,
         "data": {"turn_id": "t", "request_id": "r", "trigger": "user"}},
        {"type": "user/message", "seq": 1, "time": 2, "message_id": "u",
         "data": {"content": [{"type": "text", "text": "legacy"}], "metadata": {}, "request_id": "r"}},
        {"type": "assistant/chunk", "seq": 2, "time": 3, "message_id": "a",
         "data": {"kind": "text", "delta": "partial", "block_id": "b"}},
    ]
    legacy.write_text(
        json.dumps({"v": 1, "format": "ftre-session-log"})
        + "\n"
        + "\n".join(json.dumps(event) for event in events)
        + "\n",
        encoding="utf-8",
    )
    await manager.log(sid)
    assert not legacy.exists()
    messages = await manager.get_messages_by_session(sid)
    assert [message["id"] for message in messages] == ["u", "a"]
    assert messages[-1]["finished_reason"] == "interrupted"
    await manager.close()

    restarted = SessionService(str(tmp_path / "sessions.db"), sessions_dir=str(sessions_dir))
    await restarted.init()
    assert len(await restarted.get_messages_by_session(sid)) == 2
    assert list((await restarted.log(sid)).events) == []
    await restarted.close()


def test_concurrent_append_is_ordered_in_memory():
    log = _mk_log()
    seen: list[int] = []
    log.subscribe(lambda event: seen.append(event["seq"]))
    for _ in range(100):
        log.append("turn/retry", {
            "turn_id": "t", "code": "x", "message": "y",
            "attempt": 1, "max_attempts": 2,
        })
    assert seen == list(range(100))


@pytest.mark.asyncio
async def test_snapshot_close_drains_dirty_state(tmp_path: Path):
    manager = SessionService(str(tmp_path / "sessions.db"), sessions_dir=str(tmp_path / "sessions"))
    await manager.init()
    sid = await manager.create_session("ws")
    await manager.append_event(
        sid, "assistant/chunk", {"kind": "text", "delta": "x"}, message_id="a"
    )
    await manager.close()
    assert (tmp_path / "sessions" / sid / "session.json").exists()
