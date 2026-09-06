"""Session 事件日志提交面等价不变量测试（PRD-F43）。

whole-value 事件语义：每条 append_event 即一次原子提交，顺序由事件序决定；
错误（session 不存在 / 未知事件类型 / request_id 冲突）在单条提交时同步抛出，
天然无部分更新。本文件覆盖：
①并发 append_event 不丢事件且 seq 连续；
②write_event_log_atomic 拒绝 seq 断档（write-behind / repair / torn-tail 的
  完整能力矩阵见 tests/test_event_log_pipeline.py，此处只做轻量回归）。
"""
import asyncio
import time

import pytest
import pytest_asyncio
from ftre_agent.message import AssistantMsg

from ftre.services.session.persistence.jsonl import (
    EventLogError,
    read_event_log,
    write_event_log_atomic,
)
from ftre.services.session.service import SessionService


@pytest_asyncio.fixture
async def manager(tmp_path):
    mgr = SessionService(str(tmp_path / "sessions.db"))
    await mgr.init()
    yield mgr
    await mgr.close()


async def _wait_lines(path, minimum: int, timeout: float = 5.0) -> int:
    """轮询等待事件日志行数（含 header）达到 minimum。

    write-behind 批窗口（200ms）内 flush()/close() 不等待 in-flight 批
    （已知 src 限制，见 test_session_manager_baseline.py 注释），落盘
    断言只能等窗口自然过期。
    """
    deadline = time.monotonic() + timeout
    while True:
        if path.exists():
            count = len(
                [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            )
            if count >= minimum:
                return count
        if time.monotonic() > deadline:
            raise AssertionError(f"事件日志未在超时内达到 {minimum} 行: {path}")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_concurrent_append_event_loses_nothing_and_seq_contiguous(manager):
    sid = await manager.create_session("ws")
    # 暖缓存：log() 懒加载无锁，并发首触会互相覆盖 SessionLog 实例
    # （已知 src 限制，见 test_session_manager_concurrency.py 注释）
    await manager.get_messages_by_session(sid)

    messages = [AssistantMsg(name="default", content=f"m{i}") for i in range(20)]
    await asyncio.gather(
        *(
            manager.append_event(
                sid,
                "assistant/message",
                {"message": m.model_dump(mode="json")},
                message_id=m.id,
            )
            for m in messages
        )
    )

    # 内存权威态：事件一个不少，seq 0..N-1 连续（append-only，无覆盖）
    log = await manager.log(sid)
    events = list(log.events)
    assert len(events) == 20
    assert [e["seq"] for e in events] == list(range(20))

    # 派生消息顺序 = 事件序（whole-value 逐条替换不重排）
    derived = await manager.get_messages_by_session(sid)
    assert [m["id"] for m in derived] == [m.id for m in messages]

    # 落盘后读回仍不丢、不重排
    jsonl = manager.session_dir(sid) / "session.jsonl"
    await _wait_lines(jsonl, minimum=21)  # header + 20
    stored = read_event_log(manager.session_dir(sid))
    assert [e["message_id"] for e in stored] == [m.id for m in messages]
    assert [e["seq"] for e in stored] == list(range(20))


def test_write_event_log_atomic_rejects_seq_gap(tmp_path):
    """轻量回归：seq 断档必须整体拒绝，不落半份日志。"""
    session_dir = tmp_path / "sessions" / "ws_sess_x"
    good = [
        {"type": "assistant/message", "seq": 0, "time": 1, "message_id": "m0", "data": {}},
        {"type": "assistant/message", "seq": 1, "time": 2, "message_id": "m1", "data": {}},
    ]
    write_event_log_atomic(session_dir, good)
    assert [e["seq"] for e in read_event_log(session_dir)] == [0, 1]

    gap_dir = tmp_path / "sessions" / "ws_sess_gap"
    gapped = [
        {"type": "assistant/message", "seq": 0, "time": 1, "message_id": "m0", "data": {}},
        {"type": "assistant/message", "seq": 2, "time": 2, "message_id": "m2", "data": {}},
    ]
    with pytest.raises(EventLogError, match="seq 不连续"):
        write_event_log_atomic(gap_dir, gapped)
    assert not (gap_dir / "session.jsonl").exists()
