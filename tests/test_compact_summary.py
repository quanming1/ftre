"""CompactManager 滚动摘要改造测试（设计文档 §12 / §18.4）。

验收标准：
- 连续两次 compact 后只有一个 active summary，cursor 前进；
- 所有原始 Msg 仍存在（transcript 不含 synthetic summary）；
- LLM 失败 / 摘要膨胀时状态不变；
- compact 期间新增消息保留在 summary tail；
- compact start/done/failed WebSocket 通知保持。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from ftre_agent_core.message import AssistantMsg, UserMsg

from ftre.agent.compact_manager import CompactManager
from ftre.session.manager import SessionManager


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        llm=SimpleNamespace(context_window=100000),
        context=SimpleNamespace(compact_threshold=0.7, silent=True),
    )


@pytest_asyncio.fixture
async def env(tmp_path):
    manager = SessionManager(str(tmp_path / "sessions.db"))
    await manager.init()
    bus = AsyncMock()
    compact = CompactManager(session_manager=manager, bus=bus)
    yield manager, bus, compact
    await manager.close()


async def _seed(manager, sid, turns: int) -> list[str]:
    ids = []
    for index in range(turns):
        user = UserMsg(
            name="default", content=f"u{index} " + "内容" * 20,
            metadata={"hide": False},
            created_at=f"2026-07-27T20:{index:02d}:00+08:00",
        )
        assistant = AssistantMsg(
            name="default", content=f"a{index} " + "回复" * 20,
            created_at=f"2026-07-27T20:{index:02d}:30+08:00",
        )
        await manager.save_message(sid, user)
        await manager.save_message(sid, assistant)
        ids.extend([user.id, assistant.id])
    return ids


def _notify_types(bus) -> list[str]:
    return [
        call.args[0].data["type"]
        for call in bus.publish_outbound.call_args_list
    ]


@pytest.mark.asyncio
async def test_first_compact_creates_summary_outside_transcript(env, monkeypatch):
    manager, bus, compact = env
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 3)
    monkeypatch.setattr(
        compact, "_run_compact_llm", AsyncMock(return_value="滚动摘要 v1")
    )

    result = await compact.compact(sid, "ws", config=_config(), trigger="manual")

    assert result == "滚动摘要 v1"
    summary = await manager.get_summary(sid)
    assert summary is not None
    assert summary.message.get_text_content() == "滚动摘要 v1"
    assert summary.through_message_id == ids[-1]
    assert summary.message.metadata["context_compact"]["mode"] == "summary"
    # transcript：原始 Msg 全在，无 summary 混入
    full = await manager.get_messages_by_session(sid)
    assert [m["id"] for m in full] == ids
    # 通知
    types = _notify_types(bus)
    assert "context_compact_start" in types
    assert "context_compact_done" in types


@pytest.mark.asyncio
async def test_second_compact_replaces_summary_and_advances_cursor(env, monkeypatch):
    manager, bus, compact = env
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 2)
    llm = AsyncMock(return_value="摘要 v1")
    monkeypatch.setattr(compact, "_run_compact_llm", llm)
    await compact.compact(sid, "ws", config=_config())

    ids += await _seed_more(manager, sid, start=2, turns=2)
    llm.return_value = "摘要 v2"
    await compact.compact(sid, "ws", config=_config())

    summary = await manager.get_summary(sid)
    assert summary.message.get_text_content() == "摘要 v2"
    assert summary.through_message_id == ids[-1]
    # 第二次调用 LLM 时带上了第一次的摘要（滚动）
    assert llm.await_args_list[-1].kwargs["previous_summary"] == "摘要 v1"
    # 原始消息全部保留
    assert len(await manager.get_messages_by_session(sid)) == 8
    # LLM 上下文只剩 summary + 空 tail
    context = await manager.get_context_messages(sid)
    assert len(context) == 1
    assert context[0]["role"] == "system"


async def _seed_more(manager, sid, *, start: int, turns: int) -> list[str]:
    ids = []
    for index in range(start, start + turns):
        user = UserMsg(
            name="default", content=f"u{index} " + "内容" * 20,
            metadata={"hide": False},
            created_at=f"2026-07-27T21:{index:02d}:00+08:00",
        )
        assistant = AssistantMsg(
            name="default", content=f"a{index} " + "回复" * 20,
            created_at=f"2026-07-27T21:{index:02d}:30+08:00",
        )
        await manager.save_message(sid, user)
        await manager.save_message(sid, assistant)
        ids.extend([user.id, assistant.id])
    return ids


@pytest.mark.asyncio
async def test_llm_failure_keeps_state_unchanged(env, monkeypatch):
    manager, bus, compact = env
    sid = await manager.create_session("ws")
    await _seed(manager, sid, 2)
    monkeypatch.setattr(compact, "_run_compact_llm", AsyncMock(return_value=None))

    result = await compact.compact(sid, "ws", config=_config())

    assert result is None
    assert await manager.get_summary(sid) is None
    assert len(await manager.get_messages_by_session(sid)) == 4
    assert "context_compact_failed" in _notify_types(bus)


@pytest.mark.asyncio
async def test_inflated_summary_rejected(env, monkeypatch):
    manager, bus, compact = env
    sid = await manager.create_session("ws")
    await _seed(manager, sid, 2)
    monkeypatch.setattr(
        compact,
        "_run_compact_llm",
        AsyncMock(return_value="膨胀" * 100000),
    )

    result = await compact.compact(sid, "ws", config=_config())

    assert result is None
    assert await manager.get_summary(sid) is None
    assert len(await manager.get_messages_by_session(sid)) == 4
    assert "context_compact_failed" in _notify_types(bus)


@pytest.mark.asyncio
async def test_compact_with_existing_summary_but_no_tail_skips(env, monkeypatch):
    manager, bus, compact = env
    sid = await manager.create_session("ws")
    await _seed(manager, sid, 2)
    monkeypatch.setattr(
        compact, "_run_compact_llm", AsyncMock(return_value="摘要 v1")
    )
    await compact.compact(sid, "ws", config=_config())
    # 没有新消息，再次 compact 应静默跳过，不调 LLM
    llm = compact._run_compact_llm
    llm.reset_mock()
    result = await compact.compact(sid, "ws", config=_config())
    assert result is None
    llm.assert_not_called()
    assert (await manager.get_summary(sid)).message.get_text_content() == "摘要 v1"


@pytest.mark.asyncio
async def test_message_arriving_during_compact_stays_in_tail(env, monkeypatch):
    manager, bus, compact = env
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 2)

    async def slow_llm(*args, **kwargs):
        await asyncio.sleep(0.05)
        return "并发摘要"

    monkeypatch.setattr(compact, "_run_compact_llm", slow_llm)

    async def new_message():
        await asyncio.sleep(0.01)
        await manager.save_message(
            sid,
            UserMsg(
                name="default", content="compact 期间的新消息",
                metadata={"hide": False},
                created_at="2026-07-27T22:00:00+08:00",
            ),
        )

    await asyncio.gather(
        compact.compact(sid, "ws", config=_config()),
        new_message(),
    )

    summary = await manager.get_summary(sid)
    # 游标只覆盖 compact 开始时的最后一条
    assert summary.through_message_id == ids[-1]
    context = await manager.get_context_messages(sid)
    assert [m["content"][0]["text"] for m in context[1:]] == ["compact 期间的新消息"]
    # 完整历史 5 条
    assert len(await manager.get_messages_by_session(sid)) == 5


@pytest.mark.asyncio
async def test_compress_fast_respects_summary_cursor(env):
    manager, bus, compact = env
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 2)
    # 手动建立摘要游标到最后一条：活跃区间为空，无可裁剪
    from ftre_agent_core.message import SystemMsg

    await manager.save_summary(
        sid,
        SystemMsg(
            name="context_compact",
            content="s",
            metadata={"context_compact": {"mode": "summary"}},
        ),
        through_message_id=ids[-1],
    )
    changed = await compact.compress_fast(sid, "ws", config=SimpleNamespace())
    assert changed is False
    manager.update_message = AsyncMock()  # type: ignore[method-assign]
    changed = await compact.compress_fast(sid, "ws", config=SimpleNamespace())
    manager.update_message.assert_not_called()  # type: ignore[attr-defined]
