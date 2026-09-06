"""transcript 与 model context 分离测试（事件日志派生，PRD-F43）。

消息写入入口：append_user_message_if_absent（request_id 幂等种子）、
append_event("assistant/message", whole-value)、
append_event("compact/message", {mode:"summary", ...})。

语义要点：compact 锚点按事件位置切 tail——锚点之前的历史不进入
LLM 上下文，锚点之后的 turn 是 tail。因此测试里 compact 事件必须插在
"被覆盖轮"与"tail 轮"之间。

验收标准（语义不变）：
- Desktop（get_messages_by_session）返回完整历史，包含 compact Msg（hide=True）；
- LLM（get_context_messages）只收到最后一条 compact Msg + tail；
- compact 后 token 统计明显下降。
"""
import pytest
import pytest_asyncio
from ftre_agent.message import AssistantMsg, MsgName, UserMsg

from ftre.services.session.message.converter import to_openai
from ftre.services.session.service import SessionService as SessionManager


@pytest_asyncio.fixture
async def manager(tmp_path):
    mgr = SessionManager(str(tmp_path / "sessions.db"))
    await mgr.init()
    yield mgr
    await mgr.close()


async def _seed(manager, sid, turns: int, *, tag: str = "ctx", start: int = 0) -> list[str]:
    """每轮 user + assistant，返回本轮按序 message id 列表。

    tag：request_id 前缀（同 session 多段播种必须换 tag——request_id
    幂等指纹含 TextBlock 随机 id，同 tag 重放会抛错）。
    start：文本编号偏移（多段播种时保持全文本文本唯一，便于
    "被覆盖内容不出现"类断言）。
    """
    ids = []
    for index in range(turns):
        no = start + index
        user = UserMsg(name=MsgName.DEFAULT, content=f"u{no}", metadata={"hide": False})
        event = await manager.append_user_message_if_absent(
            sid,
            request_id=f"{tag}-{index}",
            content=user.model_dump(mode="json")["content"],
            metadata=dict(user.metadata or {}),
        )
        assert event is not None
        ids.append(str(event["message_id"]))
        assistant = AssistantMsg(name=MsgName.DEFAULT, content=f"a{no}")
        await manager.append_event(
            sid,
            "assistant/message",
            {"message": assistant.model_dump(mode="json")},
            message_id=assistant.id,
        )
        ids.append(assistant.id)
    return ids


async def _compact(manager, sid, text: str, through_message_id: str) -> None:
    await manager.append_event(
        sid,
        "compact/message",
        {
            "mode": "summary",
            "summary_text": text,
            "through_message_id": through_message_id,
        },
        message_id=f"compact_{through_message_id}",
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
    # 前两轮（将被摘要覆盖）→ compact 锚点 → tail 两轮
    covered = await _seed(manager, sid, 2, tag="covered")
    await _compact(manager, sid, "前两轮摘要", covered[3])
    tail = await _seed(manager, sid, 2, tag="tail")

    context = await manager.get_context_messages(sid)
    assert len(context) == 5
    assert context[0]["role"] == "user"
    assert context[0]["name"] == MsgName.COMPACT
    assert context[0]["metadata"]["context_compact"]["through_message_id"] == covered[3]
    assert [m["id"] for m in context[1:]] == tail

    # Desktop 完整历史包含 compact Msg（hide=True），原始消息全部保留
    full = await manager.get_messages_by_session(sid)
    assert len(full) == 9  # 8 原始 + 1 compact
    assert all(orig_id in [m["id"] for m in full] for orig_id in covered + tail)
    compact_in_full = [m for m in full if m["name"] == MsgName.COMPACT]
    assert len(compact_in_full) == 1
    assert compact_in_full[0]["metadata"]["hide"] is True


@pytest.mark.asyncio
async def test_summary_rolling_replacement(manager):
    sid = await manager.create_session("ws")
    first = await _seed(manager, sid, 1, tag="first")          # u0, a0
    await _compact(manager, sid, "摘要 v1", first[1])          # v1 覆盖 u0..a0
    second = await _seed(manager, sid, 2, tag="second")        # u1, a1, u2, a2
    await _compact(manager, sid, "摘要 v2", second[3])         # v2 覆盖到 a2
    third = await _seed(manager, sid, 1, tag="third")          # u3, a3

    # 最后一条 compact 摘要是 v2，through_message_id 指向 a2
    full = await manager.get_messages_by_session(sid)
    compact = _last_compact(full)
    assert compact is not None
    assert compact["content"][0]["text"] == "摘要 v2"
    assert compact["metadata"]["context_compact"]["through_message_id"] == second[3]

    context = await manager.get_context_messages(sid)
    assert [m["id"] for m in context[1:]] == third
    # 原始消息全部保留（8 原始 + 2 compact = 10）
    assert len(full) == 10


@pytest.mark.asyncio
async def test_context_messages_convert_to_provider_summary_plus_tail(manager):
    sid = await manager.create_session("ws")
    covered = await _seed(manager, sid, 1, tag="covered", start=0)  # u0, a0
    await _compact(manager, sid, "kept summary", covered[1])
    await _seed(manager, sid, 2, tag="tail", start=1)               # u1, a1, u2, a2

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
    await _seed(manager, sid, 5, tag="covered")
    before = await manager.get_token_usage(sid)

    ids = [m["id"] for m in await manager.get_messages_by_session(sid)]
    await _compact(manager, sid, "短摘要", ids[9])  # 覆盖前 5 轮
    await _seed(manager, sid, 1, tag="tail")
    after = await manager.get_token_usage(sid)
    assert after["total"] < before["total"]
