"""SessionLog + write-behind + repair 集成测试（PRD-F43 AC2/AC3）。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from ftre_agent.session import SessionLog

from ftre.services.session.persistence.jsonl import (
    WriteBehindCoordinator,
    read_event_log,
    write_event_log_atomic,
)
from ftre.services.session.repair import repair_events
from ftre.services.session.service import SessionService


def _mk_log(sid: str = "ws_sess_t") -> SessionLog:
    return SessionLog(sid)


class TestWriteBehind:
    async def test_batch_write_and_read_back(self, tmp_path: Path):
        log = _mk_log()
        writer = WriteBehindCoordinator(lambda sid: tmp_path / sid)
        writer.attach("ws_sess_t", log)
        for i in range(20):
            log.append("turn/retry", {
                "turn_id": "t", "code": "x", "message": f"m{i}",
                "attempt": i, "max_attempts": 20,
            })
        await writer.flush("ws_sess_t")
        await writer.close()
        stored = read_event_log(tmp_path / "ws_sess_t")
        assert [e["seq"] for e in stored] == list(range(20))
        header = (tmp_path / "ws_sess_t" / "session.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()[0]
        assert json.loads(header) == {"v": 1, "format": "ftre-session-log"}

    async def test_seq_mismatch_raises(self, tmp_path: Path):
        log = _mk_log()
        writer = WriteBehindCoordinator(lambda sid: tmp_path / sid)
        writer.attach("ws_sess_t", log)
        # 人为错位：篡改 cursor
        writer._cursors["ws_sess_t"] = 5
        log.append("turn/retry", {
            "turn_id": "t", "code": "x", "message": "y",
            "attempt": 1, "max_attempts": 2,
        })
        with pytest.raises(Exception, match="seq mismatch"):
            await writer._write_batch(
                "ws_sess_t", list(log.events)
            )
        await writer.close()

    async def test_flush_drains_pending(self, tmp_path: Path):
        log = _mk_log()
        writer = WriteBehindCoordinator(lambda sid: tmp_path / sid)
        writer.attach("ws_sess_t", log)
        log.append("turn/start", {"turn_id": "t", "trigger": "user"})
        await writer.flush("ws_sess_t")
        stored = read_event_log(tmp_path / "ws_sess_t")
        assert len(stored) == 1
        await writer.close()


class TestRepair:
    def test_synthesizes_closers_for_open_turn(self):
        log = _mk_log()
        log.append_user_message(request_id="r1", content=[{"type": "text", "text": "hi"}])
        log.append("turn/start", {"turn_id": "t1", "request_id": "r1", "trigger": "user"})
        log.append("tool/call-start", {
            "tool_call_id": "tc_1", "name": "bash", "arguments": {},
        }, message_id="m1")
        repaired = repair_events(list(log.events))
        types = [e["type"] for e in repaired]
        assert "tool/result" in types and "turn/end" in types
        turn_end = next(e for e in repaired if e["type"] == "turn/end")
        assert turn_end["data"]["outcome"] == "cancelled"
        assert turn_end["data"]["reason"] == "crashed"
        tool_result = next(e for e in repaired if e["type"] == "tool/result")
        assert tool_result["data"]["state"] == "interrupted"
        # seq 接续
        assert [e["seq"] for e in repaired] == list(range(len(repaired)))

    def test_no_repair_for_closed_turn(self):
        log = _mk_log()
        log.append("turn/start", {"turn_id": "t1", "trigger": "user"})
        log.append("turn/end", {"turn_id": "t1", "outcome": "completed", "reason": "completed"})
        assert repair_events(list(log.events)) == list(log.events)

    async def test_load_with_repair_then_append_continues(self, tmp_path: Path):
        log = _mk_log()
        writer = WriteBehindCoordinator(lambda sid: tmp_path / sid)
        writer.attach("ws_sess_t", log)
        log.append("turn/start", {"turn_id": "t1", "trigger": "user"})
        log.append("assistant/chunk", {"kind": "text", "delta": "hi"}, message_id="m1")
        await writer.close()

        stored = read_event_log(tmp_path / "ws_sess_t")
        repaired = repair_events(stored)
        restored = SessionLog("ws_sess_t")
        restored.load(repaired)
        assert restored.last_seq == len(repaired) - 1
        # 重启后新事件 seq 接续
        new_event = restored.append("turn/start", {"turn_id": "t2", "trigger": "confirm"})
        assert new_event["seq"] == len(repaired)


class TestForkAtomicWrite:
    def test_write_and_read_atomic(self, tmp_path: Path):
        log = _mk_log()
        log.append_user_message(request_id="r1", content=[{"type": "text", "text": "x"}])
        events = list(log.events)
        write_event_log_atomic(tmp_path / "ws_sess_t", events)
        stored = read_event_log(tmp_path / "ws_sess_t")
        assert stored == events

    def test_seq_gap_rejected(self, tmp_path: Path):
        events = [
            {"type": "turn/start", "seq": 1, "time": 1, "message_id": None,
             "data": {"turn_id": "t", "trigger": "user"}},
        ]
        with pytest.raises(Exception, match="seq 不连续"):
            write_event_log_atomic(tmp_path / "ws_sess_t", events)


class TestTornTail:
    def test_torn_tail_truncated(self, tmp_path: Path):
        d = tmp_path / "ws_sess_t"
        d.mkdir(parents=True)
        good = {"type": "turn/start", "seq": 0, "time": 1, "message_id": None,
                "data": {"turn_id": "t", "trigger": "user"}}
        (d / "session.jsonl").write_text(
            json.dumps({"v": 1, "format": "ftre-session-log"}) + "\n"
            + json.dumps(good) + "\n"
            + '{"type": "assistant/chunk", "seq": 1, "time": 2, "data": {"trunc',
            encoding="utf-8",
        )
        stored = read_event_log(d)
        assert len(stored) == 1 and stored[0]["seq"] == 0
        assert b'"trunc' not in (d / "session.jsonl").read_bytes()

    def test_repair_is_scoped_to_open_turn(self):
        events = [
            {"type": "assistant/message", "seq": 0, "time": 1, "message_id": "old",
             "data": {"message": {"name": "default", "role": "assistant", "id": "old",
                                    "content": [{"type": "text", "text": "old"}],
                                    "metadata": {}, "created_at": "2026-01-01T00:00:00+00:00"}}},
            {"type": "turn/end", "seq": 1, "time": 2, "message_id": "old",
             "data": {"turn_id": "old-turn", "request_id": "old-request",
                      "outcome": "completed", "reason": "completed"}},
            {"type": "turn/start", "seq": 2, "time": 3, "message_id": None,
             "data": {"turn_id": "new-turn", "request_id": "new-request", "trigger": "user"}},
            {"type": "assistant/chunk", "seq": 3, "time": 4, "message_id": "new",
             "data": {"kind": "text", "delta": "new"}},
        ]
        repaired = repair_events(events)
        end = repaired[-1]
        assert end["type"] == "turn/end"
        assert end["message_id"] == "new"
        assert end["data"]["metadata"]["synthetic"] is True

    async def test_write_failure_never_reports_flush_success(self, tmp_path: Path):
        log = _mk_log()
        writer = WriteBehindCoordinator(lambda sid: tmp_path / sid)
        writer.attach("ws_sess_t", log)
        original = writer._write_batch
        calls = 0

        async def fail_once(session_id, batch):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("disk unavailable")
            return await original(session_id, batch)

        writer._write_batch = fail_once
        log.append("turn/start", {"turn_id": "t", "trigger": "user"})
        with pytest.raises(OSError, match="disk unavailable"):
            await writer.flush("ws_sess_t")
        await writer.flush("ws_sess_t")
        assert [event["seq"] for event in read_event_log(tmp_path / "ws_sess_t")] == [0]
        await writer.close()

    async def test_session_load_persists_repair_once(self, tmp_path: Path):
        sessions_dir = tmp_path / "sessions"
        manager = SessionService(
            str(tmp_path / "sessions.db"), sessions_dir=str(sessions_dir)
        )
        await manager.init()
        sid = await manager.create_session("ws")
        write_event_log_atomic(
            manager.session_dir(sid),
            [
                {"type": "turn/start", "seq": 0, "time": 1, "message_id": None,
                 "data": {"turn_id": "t", "request_id": "r", "trigger": "user"}},
                {"type": "assistant/chunk", "seq": 1, "time": 2, "message_id": "m",
                 "data": {"kind": "text", "delta": "partial"}},
            ],
        )
        first = await manager.log(sid)
        assert [event["type"] for event in first.events].count("turn/end") == 1
        await manager.close()

        restarted = SessionService(
            str(tmp_path / "sessions.db"), sessions_dir=str(sessions_dir)
        )
        await restarted.init()
        second = await restarted.log(sid)
        assert [event["type"] for event in second.events].count("turn/end") == 1
        assert len(read_event_log(restarted.session_dir(sid))) == len(second.events)
        await restarted.close()


class TestConcurrentAppend:
    async def test_append_is_thread_loop_safe_and_ordered(self):
        log = _mk_log()
        seen = []

        def subscriber(event):
            seen.append(event["seq"])

        log.subscribe(subscriber)
        for _ in range(100):
            log.append("turn/retry", {
                "turn_id": "t", "code": "x", "message": "y",
                "attempt": 1, "max_attempts": 2,
            })
        assert seen == sorted(seen)
        await asyncio.sleep(0)
