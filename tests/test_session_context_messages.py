"""transcript 与 model context 分离测试（设计文档 §8.3 / §12.1 / §18.4）。

验收标准：
- Desktop（get_messages_by_session）返回完整历史，包含 compact Msg（hide=True）；
- LLM（get_context_messages）只收到最后一条 compact Msg + tail；
- compact 后 token 统计明显下降。
"""
import pytest
import pytest_asyncio
from ftre_agent_core.message import AssistantMsg, MsgName, UserMsg

from ftre.session import SessionManager
from ftre.session.message.converter import to_openai


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
            name=MsgName.DEFAULT, content=f"u{index}",
            metadata={"hide": False},
            created_at=f"2026-07-27T20:{index:02d}:00+08:00",
        )
        assistant = AssistantMsg(
            name=MsgName.DEFAULT, content=f"a{index}",
            created_at=f"2026-07-27T20:{index:02d}:30+08:00",
        )
        await manager.save_message(sid, user)
        await manager.save_message(sid, assistant)
        ids.extend([user.id, assistant.id])
    return ids


def _compact(text: str, through_message_id: str) -> UserMsg:
    """创建一条 compact 摘要 Msg（role=user, name=compact, hide=True）。"""
    return UserMsg(
        name=MsgName.COMPACT,
        content=text,
        metadata={
            "hide": True,
            "context_compact": {
                "mode": "summary",
                "trigger": "idle",
                "through_message_id": through_message_id,
            },
        },
    )


def _last_compact(messages: list[dict]) -> dict | None:
    """从 messages 列表中找到最后一条 compact Msg。"""
    for msg in reversed(messages):
        if msg.get("name") == MsgName.COMPACT:
            return msg
    return None


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
    await manager.save_message(sid, _compact("前两轮摘要", ids[3]))

    context = await manager.get_context_messages(sid)
    assert len(context) == 5
    assert context[0]["role"] == "user"
    assert context[0]["name"] == MsgName.COMPACT
    assert context[0]["metadata"]["context_compact"]["mode"] == "summary"
    assert [m["id"] for m in context[1:]] == ids[4:]

    # Desktop 完整历史包含 compact Msg（hide=True），原始消息全部保留
    full = await manager.get_messages_by_session(sid)
    assert len(full) == 9  # 8 原始 + 1 compact
    assert all(orig_id in [m["id"] for m in full] for orig_id in ids)
    compact_in_full = [m for m in full if m["name"] == MsgName.COMPACT]
    assert len(compact_in_full) == 1
    assert compact_in_full[0]["metadata"]["hide"] is True


@pytest.mark.asyncio
async def test_summary_rolling_replacement(manager):
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 4)
    await manager.save_message(sid, _compact("摘要 v1", ids[1]))
    await manager.save_message(sid, _compact("摘要 v2", ids[5]))

    # 最后一条 compact 摘要是 v2，through_message_id 指向 ids[5]
    full = await manager.get_messages_by_session(sid)
    compact = _last_compact(full)
    assert compact is not None
    assert compact["content"][0]["text"] == "摘要 v2"
    assert compact["metadata"]["context_compact"]["through_message_id"] == ids[5]

    context = await manager.get_context_messages(sid)
    assert [m["id"] for m in context[1:]] == ids[6:]
    # 原始消息全部保留（8 原始 + 2 compact = 10）
    assert len(full) == 10


@pytest.mark.asyncio
async def test_context_messages_convert_to_provider_summary_plus_tail(manager):
    sid = await manager.create_session("ws")
    ids = await _seed(manager, sid, 3)
    await manager.save_message(sid, _compact("kept summary", ids[1]))

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
    await manager.save_message(
        sid,
        _compact("短摘要", ids[9]),  # 覆盖前 5 轮
    )
    after = await manager.get_token_usage(sid)
    assert after["total"] < before["total"]
