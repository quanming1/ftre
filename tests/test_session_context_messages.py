"""transcript 与 model context 分离测试（设计文档 §8.3 / §12.1 / §18.4）。

验收标准：
- Desktop（get_messages_by_session）返回完整历史，不含 synthetic summary；
- LLM（get_context_messages）只收到 summary + tail；
- compact 后 token 统计明显下降。
"""
import pytest
import pytest_asyncio
from ftre_agent_core.message import AssistantMsg, SystemMsg, UserMsg

from ftre.session.converter import to_openai
from ftre.session.manager import SessionManager


@pytest_asyncio.fixture
async def manager(tmp_path):
    mgr = SessionManager(str(tmp_path / "sessions.db"))
    await mgr.init()
    yield mgr
    await mgr.close()


async def _seed(manager, sid, turns: int) -> list[str]:
    ids = []
    for index in range(turns):
        user = UserMsg(
            name="default", content=f"u{index}",
            metadata={"hide": False},
            created_at=f"2026-07-27T20:{index:02d}:00+08:00",
        )
        assistant = AssistantMsg(
            name="default", content=f"a{index}",
            created_at=f"2026-07-27T20:{index:02d}:30+08:00",
        )
        await manager.save_message(sid, user)
        await manager.save_message(sid, assistant)
        ids.extend([user.id, assistant.id])
    return ids


def _summary(text: str) -> SystemMsg:
    return SystemMsg(
        name="context_compact",
        content=text,
        metadata={"context_compact": {"mode": "summary", "trigger": "idle"}},
    )


@pytest.mark.asyncio
async def test_context_messages_without_summary_equals_full(manager):
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 3)
    context = await manager.get_context_messages(sid)
    assert [m["id"] for m in context] == ids


@pytest.mark.asyncio
async def test_context_messages_with_summary_returns_summary_plus_tail(manager):
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 4)
    # 摘要覆盖到第 2 轮结束（a1），tail = u2,a2,u3,a3
    await manager.save_summary(sid, _summary("前两轮摘要"), through_message_id=ids[3])

    context = await manager.get_context_messages(sid)
    assert len(context) == 5
    assert context[0]["role"] == "system"
    assert context[0]["metadata"]["context_compact"]["mode"] == "summary"
    assert [m["id"] for m in context[1:]] == ids[4:]

    # Desktop 完整历史不受影响，且不包含 synthetic summary
    full = await manager.get_messages_by_session(sid)
    assert [m["id"] for m in full] == ids
    assert all(
        (m["metadata"].get("context_compact") or {}).get("mode") != "summary"
        for m in full
    )


@pytest.mark.asyncio
async def test_summary_rolling_replacement(manager):
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 4)
    await manager.save_summary(sid, _summary("摘要 v1"), through_message_id=ids[1])
    await manager.save_summary(sid, _summary("摘要 v2"), through_message_id=ids[5])

    summary = await manager.get_summary(sid)
    assert summary is not None
    assert summary.message.get_text_content() == "摘要 v2"
    assert summary.through_message_id == ids[5]

    context = await manager.get_context_messages(sid)
    assert [m["id"] for m in context[1:]] == ids[6:]
    # 原始消息全部保留
    assert len(await manager.get_messages_by_session(sid)) == 8


@pytest.mark.asyncio
async def test_context_messages_convert_to_provider_summary_plus_tail(manager):
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 3)
    await manager.save_summary(sid, _summary("kept summary"), through_message_id=ids[1])

    context = await manager.get_context_messages(sid)
    provider = to_openai(context)
    assert provider[0] == {"role": "user", "content": "[历史上下文摘要]\nkept summary"}
    # 被覆盖的 u0/a0 不出现在 provider 消息里
    rendered = str(provider)
    assert "u0" not in rendered and "a0" not in rendered
    assert "u2" in rendered and "a2" in rendered


@pytest.mark.asyncio
async def test_token_usage_drops_after_compact(manager):
    sid = await manager.create_session("ws")
    await _seed(manager, sid, 6)
    before = await manager.get_token_usage(sid)

    ids = [m["id"] for m in await manager.get_messages_by_session(sid)]
    await manager.save_summary(
        sid,
        _summary("短摘要"),
        through_message_id=ids[9],  # 覆盖前 5 轮
    )
    after = await manager.get_token_usage(sid)
    assert after["total"] < before["total"]


@pytest.mark.asyncio
async def test_get_summary_returns_copy(manager):
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 1)
    await manager.save_summary(sid, _summary("原文"), through_message_id=ids[0])

    summary = await manager.get_summary(sid)
    summary.message.content[0].text = "被调用方篡改"
    # 缓存不受影响
    again = await manager.get_summary(sid)
    assert again.message.get_text_content() == "原文"
