"""SessionService 公开 API 行为基线测试（SessionLog 事件日志架构，PRD-F43）。

只使用公开 API 和临时目录，不依赖底层存储实现，用于在存储改造前后固定对外行为：

- Session CRUD / 列表排序与过滤 / count / workspaces
- 事件提交（user/message 幂等、assistant/message whole-value）与派生读取
- 最近 N 轮分页语义（可见 user Msg、before_ts、has_more）
- token 用量 last_call_usage 策略
- metadata CRUD
- external session 映射语义

"""
import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from ftre_agent.message import (
    AssistantMsg,
    Msg,
    MsgToken,
    UserMsg,
)

from ftre.services.session.service import SessionService as SessionManager


def _future_iso(seconds: float) -> str:
    """构造一个严格晚于“现在”的 ISO 时间戳（whole-value 消息排序用）。"""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


async def _wait_next_ms(after_ms: int) -> int:
    """等待系统毫秒时钟严格越过 after_ms。

    Windows 时钟粒度可达 ~15ms，连续两次 time.time() 可能落在同一毫秒，
    而 before_ts 游标依赖 user 事件时间（毫秒截断）严格递增。
    """
    while True:
        now_ms = int(time.time() * 1000)
        if now_ms > after_ms:
            return now_ms
        await asyncio.sleep(0.005)


async def _wait_log_flushed(manager: SessionManager, sid: str, timeout: float = 5.0) -> None:
    """等待 Snapshot checkpoint 完成。"""
    await manager.flush_log(sid)
    path = manager.session_dir(sid) / "session.json"
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError("Snapshot 未在超时内落盘 session.json")
        await asyncio.sleep(0.05)



@pytest_asyncio.fixture
async def manager(tmp_path):
    mgr = SessionManager(str(tmp_path / "sessions.db"))
    await mgr.init()
    yield mgr
    await mgr.close()


def _user(text: str, *, hide: bool = False) -> Msg:
    return UserMsg(metadata={"hide": hide, "agent_id": "default"}, name="default", content=text)


def _assistant(text: str, *, token: dict | None = None) -> Msg:
    kwargs: dict = {"name": "default", "content": text}
    if token:
        kwargs["token"] = MsgToken.model_validate(token)
    return AssistantMsg(**kwargs)


async def _save_user(manager: SessionManager, sid: str, msg: Msg, request_id: str) -> str:
    """提交 user/message 事件，返回派生消息使用的 message_id。"""
    event = await manager.append_user_message_if_absent(
        sid,
        request_id=request_id,
        content=msg.model_dump(mode="json")["content"],
        metadata=dict(msg.metadata or {}),
    )
    assert event is not None, "首个 request_id 提交不应被幂等跳过"
    return str(event["message_id"])


async def _save_assistant(manager: SessionManager, sid: str, msg: Msg) -> None:
    """提交 assistant/message whole-value 事件。"""
    await manager.append_event(
        sid,
        "assistant/message",
        {"message": msg.model_dump(mode="json")},
        message_id=msg.id,
    )


# ─── Session CRUD ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_get_session(manager):
    sid = await manager.create_session("ws", title="hello", workspace="E:\\ftre")
    assert sid.startswith("ws_sess_")

    session = await manager.get_session(sid)
    assert session is not None
    assert session["id"] == sid
    assert session["channel_id"] == "ws"
    assert session["title"] == "hello"
    assert session["workspace"] == "E:\\ftre"
    assert session["metadata"] == {}
    assert isinstance(session["created_at"], float)
    assert isinstance(session["updated_at"], float)
    assert session["updated_at"] >= session["created_at"]


@pytest.mark.asyncio
async def test_create_session_requires_channel_id(manager):
    with pytest.raises(ValueError):
        await manager.create_session("")


@pytest.mark.asyncio
async def test_get_session_missing_returns_none(manager):
    assert await manager.get_session("ws_sess_missing") is None


@pytest.mark.asyncio
async def test_update_session_title_workspace_and_timestamp(manager):
    sid = await manager.create_session("ws", title="old")
    before = (await manager.get_session(sid))["updated_at"]
    await asyncio.sleep(0.02)

    await manager.update_session(sid, title="new")
    session = await manager.get_session(sid)
    assert session["title"] == "new"
    assert session["updated_at"] > before

    await manager.update_session(sid, workspace="E:\\other")
    session = await manager.get_session(sid)
    assert session["title"] == "new"
    assert session["workspace"] == "E:\\other"

    # 都为 None 时仅刷 updated_at
    await asyncio.sleep(0.02)
    before = session["updated_at"]
    await manager.update_session(sid)
    session = await manager.get_session(sid)
    assert session["title"] == "new"
    assert session["updated_at"] > before


@pytest.mark.asyncio
async def test_delete_session_removes_messages(manager):
    sid = await manager.create_session("ws")
    await _save_user(manager, sid, _user("hi"), request_id="r1")
    await manager.flush_log(sid)  # 事件日志物化到磁盘后再删除
    await manager.delete_session(sid)

    assert await manager.get_session(sid) is None
    assert await manager.get_messages_by_session(sid) == []
    assert await manager.get_session_metadata(sid) == {}
    # Session Snapshot 随会话目录一起删除（单一 session.json）
    assert not manager.session_dir(sid).exists()


# ─── 列表 / 统计 / 工作区 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_sessions_order_filter_pagination(manager):
    s1 = await manager.create_session("ws", title="a", workspace="E:\\a")
    await asyncio.sleep(0.02)
    s2 = await manager.create_session("ws", title="b", workspace="")
    await asyncio.sleep(0.02)
    s3 = await manager.create_session("cron", title="c", workspace="E:\\a")

    sessions = await manager.list_sessions()
    assert [s["id"] for s in sessions] == [s3, s2, s1]

    ws_only = await manager.list_sessions(channel_id="ws")
    assert [s["id"] for s in ws_only] == [s2, s1]
    assert await manager.count_sessions(channel_id="ws") == 2
    assert await manager.count_sessions() == 3

    in_a = await manager.list_sessions(workspace="E:\\a")
    assert {s["id"] for s in in_a} == {s1, s3}
    assert await manager.count_sessions(workspace="E:\\a") == 2

    page = await manager.list_sessions(limit=1, offset=1)
    assert len(page) == 1
    assert page[0]["id"] == s2


@pytest.mark.asyncio
async def test_list_workspaces(manager):
    await manager.create_session("ws", workspace="E:\\a")
    await asyncio.sleep(0.02)
    newer = await manager.create_session("ws", workspace="E:\\b")
    await asyncio.sleep(0.02)
    await manager.create_session("ws", workspace="E:\\a")
    await manager.create_session("cron", workspace="E:\\cron")

    workspaces = await manager.list_workspaces(channel_id="ws")
    by_path = {w["workspace"]: w for w in workspaces}
    assert by_path["E:\\a"]["session_count"] == 2
    assert by_path["E:\\b"]["session_count"] == 1
    assert "E:\\cron" not in by_path
    # 按各自最新活跃倒序：E:\a 最新
    assert workspaces[0]["workspace"] == "E:\\a"

    all_ws = await manager.list_workspaces()
    assert {w["workspace"] for w in all_ws} == {"E:\\a", "E:\\b", "E:\\cron"}
    assert newer


# ─── 事件提交 / 派生读取 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_and_get_messages_round_trip(manager):
    sid = await manager.create_session("ws")
    user = _user("问题")
    user_id = await _save_user(manager, sid, user, request_id="req-1")
    await asyncio.sleep(0.002)
    assistant = _assistant(
        "回答",
        token={
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "last_call_usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )
    # whole-value 事件的 created_at 保留原值；user 消息 created_at 来自事件时间
    assistant = assistant.model_copy(update={"created_at": _future_iso(seconds=1)})
    await _save_assistant(manager, sid, assistant)

    messages = await manager.get_messages_by_session(sid)
    assert [m["id"] for m in messages] == [user_id, assistant.id]
    first, second = messages
    assert first["session_id"] == sid
    assert first["role"] == "user"
    assert first["content"][0]["text"] == "问题"
    assert first["metadata"]["hide"] is False
    assert isinstance(first["timestamp"], float)
    # timestamp 由 created_at 派生
    assert second["timestamp"] > first["timestamp"]
    assert second["token"] == {
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "last_call_usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.mark.asyncio
async def test_assistant_message_same_id_replaces_whole_value(manager):
    """同 message_id 再 append assistant/message 是 whole-value 替换，不是错误。"""
    sid = await manager.create_session("ws")
    first = _assistant("v1")
    await _save_assistant(manager, sid, first)

    second = first.model_copy(update={"content": [{"type": "text", "text": "v2"}]})
    await _save_assistant(manager, sid, second)

    messages = await manager.get_messages_by_session(sid)
    assert len(messages) == 1
    assert messages[0]["id"] == first.id
    assert messages[0]["content"][0]["text"] == "v2"


@pytest.mark.asyncio
async def test_append_user_message_updates_last_user_text_preview(manager):
    """消息活动维护 last_user_text 会话列表预览，不 bump updated_at。"""
    sid = await manager.create_session("ws")
    assert (await manager.get_session(sid))["last_user_text"] == ""

    await manager.append_user_message_if_absent(
        sid, request_id="r1", content=[{"type": "text", "text": "你好，预览"}]
    )
    assert (await manager.get_session(sid))["last_user_text"] == "你好，预览"


@pytest.mark.asyncio
async def test_update_message_keeps_order_and_updates_fields(manager):
    sid = await manager.create_session("ws")
    first_id = await _save_user(manager, sid, _user("u1"), request_id="r1")
    second = _assistant("a1")
    await _save_assistant(manager, sid, second)

    # whole-value 替换：修改后同 message_id 再 append 一次
    second.content[0].text = "a1-updated"
    second.finished_reason = "completed"
    await _save_assistant(manager, sid, second)

    messages = await manager.get_messages_by_session(sid)
    assert [m["id"] for m in messages] == [first_id, second.id]
    assert messages[1]["content"][0]["text"] == "a1-updated"
    assert messages[1]["finished_reason"] == "completed"


@pytest.mark.asyncio
async def test_append_event_unknown_session_fails_loudly(manager):
    await manager.create_session("ws")
    ghost = _assistant("ghost")
    with pytest.raises(ValueError, match="session 不存在"):
        await manager.append_event(
            "ws_sess_missing",
            "assistant/message",
            {"message": ghost.model_dump(mode="json")},
            message_id=ghost.id,
        )


@pytest.mark.asyncio
async def test_append_user_message_idempotent_on_request_id(manager):
    """同一 request_id 重复提交（Inbox 重放）幂等跳过，不产生重复气泡。"""
    sid = await manager.create_session("ws")
    content = [{"type": "text", "text": "只此一条"}]
    first = await manager.append_user_message_if_absent(sid, request_id="r1", content=content)
    second = await manager.append_user_message_if_absent(sid, request_id="r1", content=content)
    assert first is not None
    assert second is None
    messages = await manager.get_messages_by_session(sid)
    assert len(messages) == 1


# ─── 最近 N 轮分页 ───────────────────────────────────────────────


async def _seed_turns(manager, sid: str, turns: int, *, tag: str = "turn") -> list[str]:
    """每个 turn 一条可见 user + 一条 assistant，返回 user message id 列表。

    时间戳确定性（before_ts 游标依赖）：user 消息 timestamp 来自事件 time
    （毫秒截断），assistant 保留 Msg 原生 created_at（µs 精度）。若不钉住
    assistant 时间，Windows 上会出现 a_i.ts > u_{i+1}.ts 的精度倒挂，导致
    before_ts 过滤把 a_i 误排除。本函数：
    1. 轮询保证相邻 user 事件毫秒时间严格递增（ts(u_i) < ts(u_{i+1})）；
    2. 把 assistant created_at 钉在其 user 事件毫秒 +0.5ms，
       保证 ts(u_i) < ts(a_i) < ts(u_{i+1})。

    tag：request_id 前缀。同一 session 重复播种时必须换 tag——
    request_id 幂等指纹覆盖整个 content（含 TextBlock 随机 id），
    同 request_id 不同指纹会抛 "request_id 已绑定不同内容"。
    """
    ids = []
    last_ms = -1
    for index in range(turns):
        last_ms = await _wait_next_ms(last_ms)
        event = await manager.append_user_message_if_absent(
            sid,
            request_id=f"{tag}-{index}",
            content=_user(f"u{index}").model_dump(mode="json")["content"],
            metadata={"hide": False, "agent_id": "default"},
        )
        assert event is not None, "tag 内 request_id 不应重复"
        ids.append(str(event["message_id"]))
        user_ms = int(event["time"])
        assistant = _assistant(f"a{index}").model_copy(
            update={
                "created_at": datetime.fromtimestamp(
                    user_ms / 1000 + 0.0005, tz=UTC
                ).isoformat()
            }
        )
        await _save_assistant(manager, sid, assistant)
    return ids


@pytest.mark.asyncio
async def test_recent_messages_by_turns(manager):
    sid = await manager.create_session("ws")
    user_ids = await _seed_turns(manager, sid, 5)

    messages, has_more = await manager.get_recent_messages_by_turns(sid, 2)
    assert has_more is True
    # 最近 2 轮 = u3,a3,u4,a4
    assert [m["content"][0]["text"] for m in messages] == ["u3", "a3", "u4", "a4"]
    assert messages[0]["id"] == user_ids[3]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]

    messages, has_more = await manager.get_recent_messages_by_turns(sid, 10)
    assert has_more is False
    assert len(messages) == 10


@pytest.mark.asyncio
async def test_messages_snapshot_includes_inflight_chunks_and_matching_cursor(manager):
    sid = await manager.create_session("ws")
    await _save_user(manager, sid, _user("开始"), request_id="r-inflight")
    await manager.append_event(
        sid,
        "assistant/chunk",
        {"kind": "text", "delta": "流式回答", "block_id": "b-inflight"},
        message_id="m-inflight",
    )
    await manager.append_event(
        sid,
        "assistant/chunk",
        {"kind": "thinking", "delta": "思考中", "block_id": "t-inflight"},
        message_id="m-inflight",
    )

    messages, has_more, seq = await manager.get_messages_snapshot(sid)

    assert has_more is False
    assert seq == (await manager.log(sid)).seq
    assistant = next(message for message in messages if message["role"] == "assistant")
    assert [block["text"] for block in assistant["content"] if block["type"] == "text"] == [
        "流式回答"
    ]
    assert [
        block["thinking"] for block in assistant["content"] if block["type"] == "thinking"
    ] == ["思考中"]
    assert assistant["finished_at"] is None


@pytest.mark.asyncio
async def test_recent_messages_hidden_user_not_turn_boundary(manager):
    sid = await manager.create_session("ws")
    visible_id = await _save_user(
        manager, sid, _user("visible"), request_id="r-visible"
    )
    hidden_id = await _save_user(
        manager, sid, _user("hidden", hide=True), request_id="r-hidden"
    )
    assistant = _assistant("reply")
    await _save_assistant(manager, sid, assistant)

    messages, has_more = await manager.get_recent_messages_by_turns(sid, 1)
    assert has_more is False
    # 最近 1 轮从 visible user 开始，hidden 和 assistant 都在这一轮内
    assert [m["id"] for m in messages] == [visible_id, hidden_id, assistant.id]


@pytest.mark.asyncio
async def test_recent_messages_include_active_compact_before_page(manager):
    """当前页在 compact 之后时，刷新仍必须拿到摘要气泡和上下文锚点。"""
    sid = await manager.create_session("ws")
    await _seed_turns(manager, sid, 2, tag="pre")
    await manager.append_event(
        sid,
        "compact/message",
        {
            "mode": "summary",
            "summary_text": "summary",
            "through_message_id": "m1",
        },
        message_id="compact_1",
    )
    # 同 session 二次播种必须换 tag：request_id 指纹含 TextBlock 随机 id
    await _seed_turns(manager, sid, 6, tag="post")

    messages, has_more = await manager.get_recent_messages_by_turns(sid, 5)

    assert has_more is True
    assert messages[0]["id"] == "compact_1"
    assert messages[0]["name"] == "compact"
    assert [m["id"] for m in messages].count("compact_1") == 1


@pytest.mark.asyncio
async def test_recent_messages_before_ts_cursor(manager):
    sid = await manager.create_session("ws")
    user_ids = await _seed_turns(manager, sid, 5)

    first_page, has_more = await manager.get_recent_messages_by_turns(sid, 2)
    assert has_more is True
    cursor = first_page[0]["timestamp"]

    second_page, has_more = await manager.get_recent_messages_by_turns(
        sid, 2, before_ts=cursor
    )
    assert has_more is True
    assert second_page[0]["content"][0]["text"] == "u1"
    assert second_page[-1]["content"][0]["text"] == "a2"
    assert all(m["timestamp"] < cursor for m in second_page)

    third_page, has_more = await manager.get_recent_messages_by_turns(
        sid, 2, before_ts=second_page[0]["timestamp"]
    )
    assert has_more is False
    assert [m["content"][0]["text"] for m in third_page] == ["u0", "a0"]
    assert third_page[0]["id"] == user_ids[0]


@pytest.mark.asyncio
async def test_recent_messages_no_visible_user(manager):
    sid = await manager.create_session("ws")
    await _save_assistant(manager, sid, _assistant("only assistant"))
    messages, has_more = await manager.get_recent_messages_by_turns(sid, 3)
    assert messages == []
    assert has_more is False


# ─── token 用量 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_usage_last_call_anchor_strategy(manager):
    """三次调用只取最后一次作为锚点。"""
    sid = await manager.create_session("ws")
    await _save_user(manager, sid, _user("u1"), request_id="r1")
    # 一个 Reply 内三次调用：累计 37600，最后一次 15300
    await _save_assistant(
        manager,
        sid,
        _assistant("a1", token={
            "usage": {"prompt_tokens": 37000, "completion_tokens": 600, "total_tokens": 37600},
            "last_call_usage": {"prompt_tokens": 15000, "completion_tokens": 300, "total_tokens": 15300},
        }),
    )
    await _save_user(manager, sid, _user("u2 pending"), request_id="r2")

    usage = await manager.get_token_usage(sid)
    assert usage["session_id"] == sid
    # 锚点用 last_call_usage（15300），不是累计 usage（37600）
    assert usage["last_call_usage"]["prompt_tokens"] == 15000
    assert usage["last_call_usage"]["completion_tokens"] == 300
    assert usage["last_call_usage"]["total_tokens"] == 15300
    assert usage["pending_estimated"] > 0
    assert usage["total"] == 15300 + usage["pending_estimated"]


@pytest.mark.asyncio
async def test_token_usage_no_anchor_estimates_all(manager):
    sid = await manager.create_session("ws")
    await _save_user(manager, sid, _user("没有 token 的消息"), request_id="r1")
    usage = await manager.get_token_usage(sid)
    assert usage["last_call_usage"] is None
    assert usage["total"] == usage["pending_estimated"] > 0


# ─── metadata CRUD ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_metadata_crud(manager):
    sid = await manager.create_session("ws")
    assert await manager.get_session_metadata(sid) == {}

    metadata = await manager.update_session_metadata(sid, "plan", {"step": 1})
    assert metadata == {"plan": {"step": 1}}
    assert await manager.get_session_metadata(sid) == {"plan": {"step": 1}}
    assert (await manager.get_session(sid))["metadata"] == {"plan": {"step": 1}}

    metadata = await manager.update_session_metadata(sid, "other", "x")
    assert metadata == {"plan": {"step": 1}, "other": "x"}

    metadata = await manager.update_session_metadata(sid, "plan", None)
    assert metadata == {"other": "x"}
    assert await manager.get_session_metadata(sid) == {"other": "x"}


# ─── external session 映射 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_external_session_get_or_create_semantics(manager):
    first = await manager.get_or_create_external_session(
        channel_id="octo",
        external_key="octo:2:ch_1",
        title="外部会话",
        external_data={"from_uid": "alice"},
    )
    assert first.startswith("octo_sess_")

    second = await manager.get_or_create_external_session(
        channel_id="octo",
        external_key="octo:2:ch_1",
        title="被忽略",
        external_data={"from_uid": "bob"},
    )
    assert second == first

    external = await manager.get_external_session(first)
    assert external is not None
    assert external["channel_id"] == "octo"
    assert external["external_key"] == "octo:2:ch_1"
    assert external["session_id"] == first
    assert external["external_data"] == {"from_uid": "bob"}
    assert isinstance(external["created_at"], float)
    assert external["updated_at"] >= external["created_at"]

    # 不同 external_key → 新 session
    third = await manager.get_or_create_external_session(
        channel_id="octo", external_key="octo:2:ch_2"
    )
    assert third != first

    assert await manager.get_external_session("octo_sess_unknown") is None


@pytest.mark.asyncio
async def test_external_session_requires_ids(manager):
    with pytest.raises(ValueError):
        await manager.get_or_create_external_session(channel_id="", external_key="k")
    with pytest.raises(ValueError):
        await manager.get_or_create_external_session(channel_id="octo", external_key="")


# ─── 重启恢复（init 幂等） ───────────────────────────────────────


@pytest.mark.asyncio
async def test_state_survives_reinit(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    mgr = SessionManager(db_path)
    await mgr.init()
    sid = await mgr.create_session("ws", title="持久")
    await mgr.append_user_message_if_absent(
        sid, request_id="r1", content=[{"type": "text", "text": "记住我"}]
    )
    await mgr.update_session_metadata(sid, "plan", {"a": 1})
    # Snapshot checkpoint 成功后再重启。
    await _wait_log_flushed(mgr, sid)
    await mgr.close()

    mgr2 = SessionManager(db_path)
    await mgr2.init()
    try:
        session = await mgr2.get_session(sid)
        assert session is not None
        assert session["title"] == "持久"
        assert session["metadata"] == {"plan": {"a": 1}}
        messages = await mgr2.get_messages_by_session(sid)
        assert len(messages) == 1
        assert messages[0]["content"][0]["text"] == "记住我"
    finally:
        await mgr2.close()


@pytest.mark.asyncio
async def test_close_is_idempotent(manager):
    await manager.close()
    await manager.close()
