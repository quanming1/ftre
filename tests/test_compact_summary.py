"""CompactManager CustomEvent → SessionProjection 改造测试。

验收标准（新协议）：
- compact done 由 SessionProjection 投影为 messages 中一条 user/compact Msg；
- 连续两次 compact 后有两条 compact Msg，get_context_messages 返回最后一条 + tail；
- LLM 失败 / 摘要膨胀时无 compact Msg 写入；
- compact 期间新增消息保留在最后一条 compact Msg 之后；
- start/done/failed 全部为 CustomEvent，经统一事件出口派发。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from ftre_agent_core.event import CustomEvent
from ftre_agent_core.message import AssistantMsg, MsgName, UserMsg

from ftre.agent.compact_manager import CompactManager
from ftre.agent.session_projection import SessionProjection
from ftre.session import SessionManager


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        llm=SimpleNamespace(context_window=100000),
        context=SimpleNamespace(compact_threshold=0.7),
    )


@pytest_asyncio.fixture
async def env(tmp_path):
    manager = SessionManager(str(tmp_path / "sessions.db"))
    await manager.init()
    projection = SessionProjection(manager)
    emitted: list[CustomEvent] = []

    async def emit_event(session_id, channel_id, event):
        emitted.append(event)
        return await projection.apply(session_id, event)

    compact = CompactManager(session_manager=manager, emit_event=emit_event)
    yield manager, emitted, projection, compact
    await manager.close()


@pytest.mark.asyncio
async def test_compact_generation_passes_reasoning_effort_to_handler(monkeypatch):
    """Context compaction forwards the selected LLM configuration's effort."""
    from ftre.agent import compact_manager

    captured = {}

    class FakeHandler:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def stream(self, *args, **kwargs):
            if False:
                yield None

    monkeypatch.setattr(compact_manager, "LLMHandler", FakeHandler)
    compact = CompactManager(session_manager=None, emit_event=None)
    llm = SimpleNamespace(
        model="summary-model",
        api_key="key",
        api_base="",
        api_type="completions",
        reasoning_effort="none",
    )
    config = SimpleNamespace(llm=llm, compact_llm=None)

    assert await compact._run_compact_llm(
        [{"role": "user", "content": [{"type": "text", "text": "summarize this"}]}],
        config=config,
    ) is None
    assert captured["reasoning_effort"] == "none"


async def _seed(manager, sid, turns: int) -> list[str]:
    ids = []
    for index in range(turns):
        user = UserMsg(
            name=MsgName.DEFAULT, content=f"u{index} " + "内容" * 20,
            metadata={"hide": False},
            created_at=f"2026-07-27T20:{index:02d}:00+08:00",
        )
        assistant = AssistantMsg(
            name=MsgName.DEFAULT, content=f"a{index} " + "回复" * 20,
            created_at=f"2026-07-27T20:{index:02d}:30+08:00",
        )
        await manager.save_message(sid, user)
        await manager.save_message(sid, assistant)
        ids.extend([user.id, assistant.id])
    return ids


async def _seed_more(manager, sid, *, start: int, turns: int) -> list[str]:
    ids = []
    for index in range(start, start + turns):
        user = UserMsg(
            name=MsgName.DEFAULT, content=f"u{index} " + "内容" * 20,
            metadata={"hide": False},
            created_at=f"2026-07-27T21:{index:02d}:00+08:00",
        )
        assistant = AssistantMsg(
            name=MsgName.DEFAULT, content=f"a{index} " + "回复" * 20,
            created_at=f"2026-07-27T21:{index:02d}:30+08:00",
        )
        await manager.save_message(sid, user)
        await manager.save_message(sid, assistant)
        ids.extend([user.id, assistant.id])
    return ids


def _event_names(emitted) -> list[str]:
    return [e.name for e in emitted if isinstance(e, CustomEvent)]


@pytest.mark.asyncio
async def test_first_compact_writes_compact_msg(env, monkeypatch):
    manager, emitted, projection, compact = env
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 3)
    monkeypatch.setattr(
        compact, "_run_compact_llm", AsyncMock(return_value="滚动摘要 v1")
    )

    result = await compact.compact(sid, "ws", config=_config(), trigger="manual")

    assert result == "滚动摘要 v1"
    # messages 中追加了一条 user/compact Msg
    full = await manager.get_messages_by_session(sid)
    compact_msgs = [m for m in full if m["name"] == MsgName.COMPACT]
    assert len(compact_msgs) == 1
    assert compact_msgs[0]["role"] == "user"
    assert compact_msgs[0]["content"][0]["text"] == "滚动摘要 v1"
    assert compact_msgs[0]["metadata"]["hide"] is True
    assert compact_msgs[0]["metadata"]["context_compact"]["through_message_id"] == ids[-1]
    # 原始 Msg 全在
    assert [m["id"] for m in full if m["name"] != MsgName.COMPACT] == ids
    # 事件
    names = _event_names(emitted)
    assert "context_compact_start" in names
    assert "context_compact_done" in names


@pytest.mark.asyncio
async def test_same_session_compact_requests_share_one_task(env, monkeypatch):
    manager, emitted, projection, compact = env
    sid = await manager.create_session("ws")
    await _seed(manager, sid, 2)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_llm(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "共享压缩摘要"

    monkeypatch.setattr(compact, "_run_compact_llm", slow_llm)

    first = asyncio.create_task(
        compact.compact(sid, "ws", config=_config(), trigger="idle")
    )
    await started.wait()
    second = asyncio.create_task(
        compact.compact(sid, "ws", config=_config(), trigger="manual")
    )
    await asyncio.sleep(0)

    assert compact.is_compacting(sid) is True
    release.set()
    assert await asyncio.gather(first, second) == [
        "共享压缩摘要",
        "共享压缩摘要",
    ]
    await asyncio.sleep(0)  # 让 Task done callback 清理登记

    assert calls == 1
    assert _event_names(emitted).count("context_compact_start") == 1
    assert _event_names(emitted).count("context_compact_done") == 1
    assert compact.is_compacting(sid) is False


@pytest.mark.asyncio
async def test_critical_compact_preserves_current_user_message_as_tail(env, monkeypatch):
    manager, emitted, projection, compact = env
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 2)
    current = UserMsg(
        name=MsgName.DEFAULT,
        content="current request",
        metadata={"hide": False},
    )
    await manager.save_message(sid, current)
    llm = AsyncMock(return_value="history summary")
    monkeypatch.setattr(compact, "_run_compact_llm", llm)

    await compact.compact(
        sid,
        "ws",
        config=_config(),
        trigger="auto",
        preserve_from_message_id=current.id,
    )

    summarized_records = llm.await_args.args[0]
    assert [record["id"] for record in summarized_records] == ids
    context = await manager.get_context_messages(sid)
    assert [record["name"] for record in context] == [
        MsgName.COMPACT,
        MsgName.DEFAULT,
    ]
    assert context[1]["id"] == current.id
    assert context[0]["metadata"]["context_compact"]["through_message_id"] == ids[-1]


@pytest.mark.asyncio
async def test_second_compact_keeps_both_and_context_uses_last(env, monkeypatch):
    manager, emitted, projection, compact = env
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 2)
    llm = AsyncMock(return_value="摘要 v1")
    monkeypatch.setattr(compact, "_run_compact_llm", llm)
    await compact.compact(sid, "ws", config=_config())

    ids += await _seed_more(manager, sid, start=2, turns=2)
    llm.return_value = "摘要 v2"
    await compact.compact(sid, "ws", config=_config())

    full = await manager.get_messages_by_session(sid)
    compact_msgs = [m for m in full if m["name"] == MsgName.COMPACT]
    assert len(compact_msgs) == 2
    # 第二次 LLM 调用时带上了第一次的摘要（滚动）
    assert llm.await_args_list[-1].kwargs["previous_summary"] == "摘要 v1"
    # LLM 上下文 = 最后一条 compact + tail
    context = await manager.get_context_messages(sid)
    assert context[0]["name"] == MsgName.COMPACT
    assert context[0]["content"][0]["text"] == "摘要 v2"
    # tail 是 compact 之后的消息
    assert all(m["name"] != MsgName.COMPACT for m in context[1:])


@pytest.mark.asyncio
async def test_llm_failure_writes_no_compact_msg(env, monkeypatch):
    manager, emitted, projection, compact = env
    sid = await manager.create_session("ws")
    await _seed(manager, sid, 2)
    monkeypatch.setattr(compact, "_run_compact_llm", AsyncMock(return_value=None))

    result = await compact.compact(sid, "ws", config=_config())

    assert result is None
    full = await manager.get_messages_by_session(sid)
    assert not any(m["name"] == MsgName.COMPACT for m in full)
    assert len(full) == 4
    assert "context_compact_failed" in _event_names(emitted)


@pytest.mark.asyncio
async def test_inflated_summary_rejected(env, monkeypatch):
    manager, emitted, projection, compact = env
    sid = await manager.create_session("ws")
    await _seed(manager, sid, 2)
    monkeypatch.setattr(
        compact, "_run_compact_llm", AsyncMock(return_value="膨胀" * 100000)
    )

    result = await compact.compact(sid, "ws", config=_config())

    assert result is None
    full = await manager.get_messages_by_session(sid)
    assert not any(m["name"] == MsgName.COMPACT for m in full)
    assert len(full) == 4
    assert "context_compact_failed" in _event_names(emitted)


@pytest.mark.asyncio
async def test_compact_with_existing_compact_but_no_tail_skips(env, monkeypatch):
    manager, emitted, projection, compact = env
    sid = await manager.create_session("ws")
    await _seed(manager, sid, 2)
    monkeypatch.setattr(compact, "_run_compact_llm", AsyncMock(return_value="摘要 v1"))
    await compact.compact(sid, "ws", config=_config())
    # 没有新消息，再次 compact 应静默跳过，不调 LLM
    llm = compact._run_compact_llm
    llm.reset_mock()
    result = await compact.compact(sid, "ws", config=_config())
    assert result is None
    llm.assert_not_called()
    # 仍只有一条 compact Msg
    full = await manager.get_messages_by_session(sid)
    assert len([m for m in full if m["name"] == MsgName.COMPACT]) == 1


@pytest.mark.asyncio
async def test_message_arriving_during_compact_stays_in_tail(env, monkeypatch):
    manager, emitted, projection, compact = env
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
                name=MsgName.DEFAULT, content="compact 期间的新消息",
                metadata={"hide": False},
                created_at="2026-07-27T22:00:00+08:00",
            ),
        )

    await asyncio.gather(
        compact.compact(sid, "ws", config=_config()),
        new_message(),
    )

    context = await manager.get_context_messages(sid)
    # 第一条是 compact Msg
    assert context[0]["name"] == MsgName.COMPACT
    # compact 期间的新消息在 tail
    assert context[1]["content"][0]["text"] == "compact 期间的新消息"
    # compact 的 through_message_id 只覆盖 compact 开始时的最后一条
    assert context[0]["metadata"]["context_compact"]["through_message_id"] == ids[-1]
    # 完整历史 6 条（4 原始 + 1 compact 期间新消息 + 1 compact）
    assert len(await manager.get_messages_by_session(sid)) == 6


@pytest.mark.asyncio
async def test_done_event_idempotent_no_duplicate(env, monkeypatch):
    """重放同一 context_compact_done event.id 不产生重复 Msg。"""
    manager, emitted, projection, compact = env
    sid = await manager.create_session("ws")
    await _seed(manager, sid, 1)
    from ftre_agent_core.event import CustomEvent

    event = CustomEvent(
        name="context_compact_done",
        value={
            "summary_text": "摘要",
            "mode": "summary",
            "through_message_id": "x",
            "trigger": "manual",
            "tokens_before": 100,
            "tokens_after": 10,
        },
    )
    await projection.apply(sid, event)
    await projection.apply(sid, event)  # 重放同一 event.id
    full = await manager.get_messages_by_session(sid)
    compact_msgs = [m for m in full if m["name"] == MsgName.COMPACT]
    assert len(compact_msgs) == 1


@pytest.mark.asyncio
async def test_compress_fast_no_tool_results_returns_false(env):
    manager, emitted, projection, compact = env
    sid = await manager.create_session("ws")
    # 无工具结果时返回 False，不产生任何 compact 事件
    await _seed(manager, sid, 1)
    changed = await compact.compress_fast(sid, "ws", config=SimpleNamespace())
    assert changed is False
    assert "context_compact_done" not in _event_names(emitted)


@pytest.mark.asyncio
async def test_compress_fast_projects_compact_fast_msg(env):
    """fast 压缩裁剪工具输出后投影为一条 name=compact_fast 的展示气泡 Msg，
    且不污染上下文锚点（get_context_messages 的 tail 起点只认 MsgName.COMPACT）。"""
    from ftre_agent_core.message import (
        AssistantMsg as _AssistantMsg,
        ToolCallBlock,
        ToolResultBlock,
    )

    manager, emitted, projection, compact = env
    sid = await manager.create_session("ws")

    user = UserMsg(name=MsgName.DEFAULT, content="请读取文件")
    assistant = _AssistantMsg(
        name=MsgName.DEFAULT,
        content=[
            ToolCallBlock(id="c1", name="read", arguments={}),
            ToolResultBlock(id="c1", name="read", output="很长的工具输出" * 10, state="success"),
        ],
    )
    await manager.save_message(sid, user)
    await manager.save_message(sid, assistant)

    changed = await compact.compress_fast(sid, "ws", config=SimpleNamespace(), keep_turns=0)
    assert changed is True

    # 生成了一条 name=compact_fast 的 Msg（role=assistant，无 hide）
    full = await manager.get_messages_by_session(sid)
    fast_msgs = [m for m in full if m["name"] == MsgName.COMPACT_FAST]
    assert len(fast_msgs) == 1
    assert fast_msgs[0]["role"] == "assistant"
    assert fast_msgs[0]["metadata"].get("hide") is not True
    assert fast_msgs[0]["metadata"]["context_compact"]["mode"] == "fast"
    assert "裁剪" in fast_msgs[0]["content"][0]["text"]

    # compact_fast 不是上下文锚点：无 compact Msg 时 get_context_messages 返回全部
    # （含 compact_fast 本身，作为提醒发给 LLM），但绝不把它当 tail 起点。
    context = await manager.get_context_messages(sid)
    assert not any(
        m["name"] == MsgName.COMPACT for m in context
    ), "不应存在 summary 锚点"
    # compact_fast Msg 作为普通提醒消息进入上下文
    assert any(m["name"] == MsgName.COMPACT_FAST for m in context)
