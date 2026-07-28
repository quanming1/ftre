"""SessionManager（JSON Store）并发与丢更新防护测试（设计文档 §9.3 / §18.3）。

验收标准：
- 两个并发 save_message 两条都在；
- save_message + update_metadata 两项修改都在；
- compact 期间新增消息保留在 summary tail（save_summary 基于最新 state）；
- update_message 与 delete_session 交错不损坏其他文件。
"""
import asyncio
import json

import pytest
import pytest_asyncio
from ftre_agent_core.message import SystemMsg, UserMsg

from ftre.session.manager import SessionManager


@pytest_asyncio.fixture
async def manager(tmp_path):
    mgr = SessionManager(str(tmp_path / "sessions.db"))
    await mgr.init()
    yield mgr
    await mgr.close()


def _user(text: str) -> UserMsg:
    return UserMsg(name="default", content=text, metadata={"hide": False})


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
async def test_save_summary_does_not_clobber_concurrent_messages(manager):
    sid = await manager.create_session("ws")
    first = _user("u1")
    await manager.save_message(sid, first)

    # 模拟 compact：锁外"生成摘要"期间，新消息先进来
    summary_msg = SystemMsg(
        name="context_compact",
        content="截至 u1 的摘要",
        metadata={"context_compact": {"mode": "summary"}},
    )

    async def delayed_summary():
        await asyncio.sleep(0.05)
        await manager.save_summary(sid, summary_msg, through_message_id=first.id)

    async def new_message():
        await asyncio.sleep(0.01)
        await manager.save_message(sid, _user("u2 新增"))

    await asyncio.gather(delayed_summary(), new_message())

    # 两条消息都在，摘要游标仍指向 u1，u2 留在 tail
    messages = await manager.get_messages_by_session(sid)
    assert [m["content"][0]["text"] for m in messages] == ["u1", "u2 新增"]
    summary = await manager.get_summary(sid)
    assert summary is not None
    assert summary.through_message_id == first.id

    context = await manager.get_context_messages(sid)
    assert context[0]["metadata"]["context_compact"]["mode"] == "summary"
    assert [m["content"][0]["text"] for m in context[1:]] == ["u2 新增"]


@pytest.mark.asyncio
async def test_save_summary_rejects_dangling_cursor(manager):
    sid = await manager.create_session("ws")
    await manager.save_message(sid, _user("u1"))
    with pytest.raises(ValueError, match="cursor"):
        await manager.save_summary(
            sid,
            SystemMsg(name="context_compact", content="s"),
            through_message_id="msg_missing",
        )


@pytest.mark.asyncio
async def test_save_summary_requires_system_msg(manager):
    sid = await manager.create_session("ws")
    user = _user("u1")
    await manager.save_message(sid, user)
    with pytest.raises(ValueError, match="SystemMsg"):
        await manager.save_summary(sid, _user("not system"), through_message_id=user.id)


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
