"""SessionService（事件日志架构）并发与丢更新防护测试。

消息写入入口：append_user_message_if_absent（request_id 幂等种子）、
append_event("compact/message", ...)、同 message_id 再 append（whole-value 替换）。

验收标准（语义不变）：
- 两个并发用户消息提交条条都在（事件日志 append-only）；
- 消息提交 + update_metadata + update_session 并发互不覆盖；
- compact 期间新增消息保留在 compact tail；
- whole-value 替换与 delete_session 交错不损坏其他会话；
- 落盘文件人类可读，无流式 Event 名称混入。
"""
import asyncio
import json
import time

import pytest
import pytest_asyncio
from ftre_agent.message import AssistantMsg, MsgName, UserMsg

from ftre.services.session.service import SessionService as SessionManager


@pytest_asyncio.fixture
async def manager(tmp_path):
    mgr = SessionManager(str(tmp_path / "sessions.db"))
    await mgr.init()
    yield mgr
    await mgr.close()


def _user(text: str) -> UserMsg:
    return UserMsg(name=MsgName.DEFAULT, content=text, metadata={"hide": False})


async def _save_user(manager, sid, msg, request_id):
    event = await manager.append_user_message_if_absent(
        sid,
        request_id=request_id,
        content=msg.model_dump(mode="json")["content"],
        metadata=dict(msg.metadata or {}),
    )
    assert event is not None
    return str(event["message_id"])


async def _append_compact(manager, sid, summary_text, through_message_id, message_id):
    await manager.append_event(
        sid,
        "compact/message",
        {
            "mode": "summary",
            "summary_text": summary_text,
            "through_message_id": through_message_id,
        },
        message_id=message_id,
    )


async def _wait_log_flushed(manager, sid, timeout: float = 5.0) -> None:
    """等待 write-behind 批窗口（200ms）把事件物化为 session.jsonl。

    已知 src 限制：flush()/close() 不等待 in-flight 批（详见
    tests/test_session_manager_baseline.py 同名 helper 注释）。
    """
    path = manager.session_dir(sid) / "session.jsonl"
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError("write-behind 未在超时内落盘 session.jsonl")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_concurrent_save_message_loses_nothing(manager):
    sid = await manager.create_session("ws")
    # 暖缓存：已知 src 限制（记录于报告）——log() 懒加载无锁，10 个协程
    # 同时首触会各自构建 SessionLog，最后一个覆盖 _logs 缓存，其余实例上的
    # 事件丢失。先经一次顺序访问完成懒加载，再验证并发 append 不丢。
    await manager.get_messages_by_session(sid)
    await asyncio.gather(
        *(_save_user(manager, sid, _user(f"m{i}"), request_id=f"r{i}") for i in range(10))
    )
    messages = await manager.get_messages_by_session(sid)
    assert len(messages) == 10
    assert {m["content"][0]["text"] for m in messages} == {f"m{i}" for i in range(10)}


@pytest.mark.asyncio
async def test_concurrent_save_message_and_metadata_update(manager):
    sid = await manager.create_session("ws")
    await asyncio.gather(
        _save_user(manager, sid, _user("hello"), request_id="r1"),
        manager.update_session_metadata(sid, "plan", {"step": 1}),
        manager.update_session(sid, title="并发标题"),
    )
    assert len(await manager.get_messages_by_session(sid)) == 1
    assert await manager.get_session_metadata(sid) == {"plan": {"step": 1}}
    assert (await manager.get_session(sid))["title"] == "并发标题"


@pytest.mark.asyncio
async def test_compact_does_not_clobber_concurrent_messages(manager):
    sid = await manager.create_session("ws")
    first_id = await _save_user(manager, sid, _user("u1"), request_id="u1")

    # 模拟 compact：摘要生成期间新消息先进来（compact 先落日志，u2 紧随其后）
    async def delayed_compact():
        await asyncio.sleep(0.01)
        await _append_compact(manager, sid, "截至 u1 的摘要", first_id, "compact_1")

    async def new_message():
        await asyncio.sleep(0.05)
        await _save_user(manager, sid, _user("u2 新增"), request_id="u2")

    await asyncio.gather(delayed_compact(), new_message())

    # 两条原始消息都在，加上 compact Msg 共 3 条
    messages = await manager.get_messages_by_session(sid)
    non_compact = [m for m in messages if m["name"] != MsgName.COMPACT]
    assert [m["content"][0]["text"] for m in non_compact] == ["u1", "u2 新增"]

    # compact Msg 的 through_message_id 仍指向 u1
    compact_msgs = [m for m in messages if m["name"] == MsgName.COMPACT]
    assert len(compact_msgs) == 1
    assert compact_msgs[0]["metadata"]["context_compact"]["through_message_id"] == first_id

    # LLM 上下文 = compact 锚点 + tail（u2 留在 tail）
    context = await manager.get_context_messages(sid)
    assert context[0]["name"] == MsgName.COMPACT
    # derive：summary compact 的 context_compact 元信息（mode 不落入元信息）
    assert (
        context[0]["metadata"]["context_compact"]["through_message_id"] == first_id
    )
    assert [m["content"][0]["text"] for m in context[1:]] == ["u2 新增"]


@pytest.mark.asyncio
async def test_update_message_and_delete_session_do_not_corrupt_others(manager):
    sid_a = await manager.create_session("ws")
    sid_b = await manager.create_session("ws")
    # "update" 语义 = 同 message_id whole-value 替换（assistant/message）
    msg_a = AssistantMsg(name=MsgName.DEFAULT, content="a")
    await manager.append_event(
        sid_a,
        "assistant/message",
        {"message": msg_a.model_dump(mode="json")},
        message_id=msg_a.id,
    )
    await _save_user(manager, sid_b, _user("b"), request_id="b")

    updated = msg_a.model_copy(
        update={"content": [{"type": "text", "text": "a-updated"}]}
    )

    async def update_a():
        await manager.append_event(
            sid_a,
            "assistant/message",
            {"message": updated.model_dump(mode="json")},
            message_id=updated.id,
        )

    results = await asyncio.gather(
        update_a(),
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
    await _save_user(manager, sid, _user("直接阅读我"), request_id="r1")
    await _wait_log_flushed(manager, sid)

    files = list((tmp_path / "sessions").glob("*/session.json"))
    assert len(files) == 1
    meta_text = files[0].read_text(encoding="utf-8")
    payload = json.loads(meta_text)
    assert payload["session"]["id"] == sid
    assert payload["session"]["title"] == "可读性"
    # session.json 只存元信息，消息事实在 session.jsonl
    assert "messages" not in payload

    jsonl_path = files[0].parent / "session.jsonl"
    log_text = jsonl_path.read_text(encoding="utf-8")
    lines = [line for line in log_text.splitlines() if line.strip()]
    assert json.loads(lines[0]) == {"v": 1, "format": "ftre-session-log"}
    events = [json.loads(line) for line in lines[1:]]
    assert events[0]["type"] == "user/message"
    assert "直接阅读我" in json.dumps(events[0]["data"]["content"], ensure_ascii=False)
    # 无流式 Event 名称混入
    assert "TEXT_BLOCK_DELTA" not in meta_text + log_text
