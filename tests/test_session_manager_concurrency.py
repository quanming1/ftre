"""SessionManager（JSON Store）并发与丢更新防护测试（设计文档 §9.3 / §18.3）。

验收标准：
- 两个并发 save_message 两条都在；
- save_message + update_metadata 两项修改都在；
- compact 期间新增消息保留在 compact tail（save_message 基于最新 state）；
- update_message 与 delete_session 交错不损坏其他文件。
"""
import asyncio
import json

import pytest
import pytest_asyncio
from ftre_agent.message import MsgName, UserMsg

from ftre.services.session.service import SessionService as SessionManager


@pytest_asyncio.fixture
async def manager(tmp_path):
    mgr = SessionManager(str(tmp_path / "sessions.db"))
    await mgr.init()
    yield mgr
    await mgr.close()


def _user(text: str) -> UserMsg:
    return UserMsg(name=MsgName.DEFAULT, content=text, metadata={"hide": False})


def _compact(text: str, through_message_id: str) -> UserMsg:
    """创建一条 compact 摘要 Msg（role=user, name=compact, hide=True）。"""
    return UserMsg(
        name=MsgName.COMPACT,
        content=text,
        metadata={
            "hide": True,
            "context_compact": {
                "mode": "summary",
                "through_message_id": through_message_id,
            },
        },
    )


@pytest.mark.asyncio
async def test_concurrent_save_message_loses_nothing(manager):
    sid = await manager.create_session("ws")
    await asyncio.gather(
        *(manager.save_message(sid, _user(f"m{i}")) for i in range(10))
    )
    messages = await manager.get_messages_by_session(sid)
    assert len(messages) == 10
    assert {m["content"][0]["text"] for m in messages} == {f"m{i}" for i in range(10)}


@pytest.mark.asyncio
async def test_concurrent_save_message_and_metadata_update(manager):
    sid = await manager.create_session("ws")
    await asyncio.gather(
        manager.save_message(sid, _user("hello")),
        manager.update_session_metadata(sid, "plan", {"step": 1}),
        manager.update_session(sid, title="并发标题"),
    )
    assert len(await manager.get_messages_by_session(sid)) == 1
    assert await manager.get_session_metadata(sid) == {"plan": {"step": 1}}
    assert (await manager.get_session(sid))["title"] == "并发标题"


@pytest.mark.asyncio
async def test_compact_does_not_clobber_concurrent_messages(manager):
    sid = await manager.create_session("ws")
    first = _user("u1")
    await manager.save_message(sid, first)

    # 模拟 compact：锁外"生成摘要"期间，新消息先进来
    summary_msg = _compact("截至 u1 的摘要", first.id)

    async def delayed_compact():
        await asyncio.sleep(0.05)
        await manager.save_message(sid, summary_msg)

    async def new_message():
        await asyncio.sleep(0.01)
        await manager.save_message(sid, _user("u2 新增"))

    await asyncio.gather(delayed_compact(), new_message())

    # 两条原始消息都在，加上 compact Msg 共 3 条
    messages = await manager.get_messages_by_session(sid)
    non_compact = [m for m in messages if m["name"] != MsgName.COMPACT]
    assert [m["content"][0]["text"] for m in non_compact] == ["u1", "u2 新增"]

    # compact Msg 的 through_message_id 仍指向 u1
    compact_msgs = [m for m in messages if m["name"] == MsgName.COMPACT]
    assert len(compact_msgs) == 1
    assert compact_msgs[0]["metadata"]["context_compact"]["through_message_id"] == first.id

    # LLM 上下文 = compact + tail（u2 留在 tail）
    context = await manager.get_context_messages(sid)
    assert context[0]["metadata"]["context_compact"]["mode"] == "summary"
    assert [m["content"][0]["text"] for m in context[1:]] == ["u2 新增"]


@pytest.mark.asyncio
async def test_update_message_and_delete_session_do_not_corrupt_others(manager):
    sid_a = await manager.create_session("ws")
    sid_b = await manager.create_session("ws")
    msg_a = _user("a")
    await manager.save_message(sid_a, msg_a)
    await manager.save_message(sid_b, _user("b"))

    msg_a.content[0].text = "a-updated"
    results = await asyncio.gather(
        manager.update_message(msg_a),
        manager.delete_session(sid_a),
        return_exceptions=True,
    )
    # 两种顺序都合法：update 先（成功）或 delete 先（update 报明确错误）
    for result in results:
        if isinstance(result, Exception):
            assert isinstance(result, ValueError)

    # 另一个 session 的文件完好
    messages_b = await manager.get_messages_by_session(sid_b)
    assert len(messages_b) == 1
    assert messages_b[0]["content"][0]["text"] == "b"


@pytest.mark.asyncio
async def test_state_json_human_readable(manager, tmp_path):
    sid = await manager.create_session("ws", title="可读性")
    await manager.save_message(sid, _user("直接阅读我"))
    files = list((tmp_path / "sessions").glob("*/state.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["session"]["id"] == sid
    assert payload["session"]["title"] == "可读性"
    assert payload["messages"][0]["content"][0]["text"] == "直接阅读我"
    # 无流式 Event 名称混入
    assert "TEXT_BLOCK_DELTA" not in files[0].read_text(encoding="utf-8")
