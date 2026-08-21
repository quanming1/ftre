"""SessionManager 公开 API 行为基线测试。

只使用公开 API 和临时目录，不依赖底层存储实现（SQLite / JSON Store），
用于在存储改造前后固定对外行为：

- Session CRUD / 列表排序与过滤 / count / workspaces
- Msg 保存、更新、按序读取
- 最近 N 轮分页语义（可见 user Msg、before_ts、has_more）
- token 用量 last_call_usage 策略
- metadata CRUD
- external session 映射语义
"""
import asyncio

import pytest
import pytest_asyncio
from ftre_agent_core.message import (
    AssistantMsg,
    Msg,
    MsgToken,
    UserMsg,
)

from ftre.services.session.service import SessionService as SessionManager


@pytest_asyncio.fixture
async def manager(tmp_path):
    mgr = SessionManager(str(tmp_path / "sessions.db"))
    await mgr.init()
    yield mgr
    await mgr.close()


def _user(text: str, *, hide: bool = False, created_at: str | None = None) -> Msg:
    kwargs: dict = {"name": "default", "content": text}
    if created_at:
        kwargs["created_at"] = created_at
    return UserMsg(metadata={"hide": hide, "agent_id": "default"}, **kwargs)


def _assistant(text: str, *, token: dict | None = None, created_at: str | None = None) -> Msg:
    kwargs: dict = {"name": "default", "content": text}
    if token:
        kwargs["token"] = MsgToken.model_validate(token)
    if created_at:
        kwargs["created_at"] = created_at
    return AssistantMsg(**kwargs)


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
    await manager.save_message(sid, _user("hi"))
    await manager.delete_session(sid)

    assert await manager.get_session(sid) is None
    assert await manager.get_messages_by_session(sid) == []
    assert await manager.get_session_metadata(sid) == {}


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


# ─── Msg 保存 / 更新 / 读取 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_save_and_get_messages_round_trip(manager):
    sid = await manager.create_session("ws")
    user = _user("问题", created_at="2026-07-27T20:00:00+08:00")
    assistant = _assistant(
        "回答",
        token={
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "last_call_usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
        created_at="2026-07-27T20:00:01+08:00",
    )
    returned = await manager.save_message(sid, user)
    assert returned == user.id
    await manager.save_message(sid, assistant)

    messages = await manager.get_messages_by_session(sid)
    assert [m["id"] for m in messages] == [user.id, assistant.id]
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
    # dict 输入也要支持
    await manager.save_message(sid, _user("再来一条").model_dump(mode="json"))
    assert len(await manager.get_messages_by_session(sid)) == 3


@pytest.mark.asyncio
async def test_save_message_rejects_duplicate_id(manager):
    sid = await manager.create_session("ws")
    msg = _user("dup")
    await manager.save_message(sid, msg)
    with pytest.raises(Exception):  # noqa: B017 legacy compatibility boundary reviewed in F1
        await manager.save_message(sid, msg)


@pytest.mark.asyncio
async def test_save_message_bumps_session_updated_at(manager):
    sid = await manager.create_session("ws")
    before = (await manager.get_session(sid))["updated_at"]
    await asyncio.sleep(0.02)
    await manager.save_message(sid, _user("hi"))
    assert (await manager.get_session(sid))["updated_at"] > before


@pytest.mark.asyncio
async def test_update_message_keeps_order_and_updates_fields(manager):
    sid = await manager.create_session("ws")
    first = _user("u1")
    second = _assistant("a1")
    await manager.save_message(sid, first)
    await manager.save_message(sid, second)

    second.content[0].text = "a1-updated"
    second.finished_reason = "completed"
    await manager.update_message(second)

    messages = await manager.get_messages_by_session(sid)
    assert [m["id"] for m in messages] == [first.id, second.id]
    assert messages[1]["content"][0]["text"] == "a1-updated"
    assert messages[1]["finished_reason"] == "completed"


@pytest.mark.asyncio
async def test_update_message_unknown_id_fails_loudly(manager):
    await manager.create_session("ws")
    with pytest.raises(Exception):  # noqa: B017 legacy compatibility boundary reviewed in F1
        await manager.update_message(_assistant("ghost"))


# ─── 最近 N 轮分页 ───────────────────────────────────────────────


async def _seed_turns(manager, sid, turns: int) -> list[str]:
    """每个 turn 一条可见 user + 一条 assistant，返回 user msg id 列表。"""
    ids = []
    for index in range(turns):
        user = _user(f"u{index}", created_at=f"2026-07-27T20:{index:02d}:00+08:00")
        assistant = _assistant(
            f"a{index}", created_at=f"2026-07-27T20:{index:02d}:30+08:00"
        )
        await manager.save_message(sid, user)
        await manager.save_message(sid, assistant)
        ids.append(user.id)
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
async def test_recent_messages_hidden_user_not_turn_boundary(manager):
    sid = await manager.create_session("ws")
    visible = _user("visible", created_at="2026-07-27T20:00:00+08:00")
    hidden = _user("hidden", hide=True, created_at="2026-07-27T20:01:00+08:00")
    assistant = _assistant("reply", created_at="2026-07-27T20:02:00+08:00")
    for msg in (visible, hidden, assistant):
        await manager.save_message(sid, msg)

    messages, has_more = await manager.get_recent_messages_by_turns(sid, 1)
    assert has_more is False
    # 最近 1 轮从 visible user 开始，hidden 和 assistant 都在这一轮内
    assert [m["id"] for m in messages] == [visible.id, hidden.id, assistant.id]


@pytest.mark.asyncio
async def test_recent_messages_include_active_compact_before_page(manager):
    """当前页在 compact 之后时，刷新仍必须拿到摘要气泡和上下文锚点。"""
    sid = await manager.create_session("ws")
    await _seed_turns(manager, sid, 2)
    compact = _user("summary", hide=True)
    compact.name = "compact"
    await manager.save_message(sid, compact)
    await _seed_turns(manager, sid, 6)

    messages, has_more = await manager.get_recent_messages_by_turns(sid, 5)

    assert has_more is True
    assert messages[0]["id"] == compact.id
    assert messages[0]["name"] == "compact"
    assert [m["id"] for m in messages].count(compact.id) == 1


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
    await manager.save_message(sid, _assistant("only assistant"))
    messages, has_more = await manager.get_recent_messages_by_turns(sid, 3)
    assert messages == []
    assert has_more is False


# ─── token 用量 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_usage_last_call_anchor_strategy(manager):
    """三次调用只取最后一次作为锚点。"""
    sid = await manager.create_session("ws")
    await manager.save_message(sid, _user("u1"))
    # 一个 Reply 内三次调用：累计 37600，最后一次 15300
    await manager.save_message(
        sid,
        _assistant("a1", token={
            "usage": {"prompt_tokens": 37000, "completion_tokens": 600, "total_tokens": 37600},
            "last_call_usage": {"prompt_tokens": 15000, "completion_tokens": 300, "total_tokens": 15300},
        })
    )
    await manager.save_message(sid, _user("u2 pending"))

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
    await manager.save_message(sid, _user("没有 token 的消息"))
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
    await mgr.save_message(sid, _user("记住我"))
    await mgr.update_session_metadata(sid, "plan", {"a": 1})
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
