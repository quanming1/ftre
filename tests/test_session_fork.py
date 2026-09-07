"""fork_session 行为测试（Msg Snapshot 深拷贝，PRD-F44）。

fork 完整拷贝 Msg Snapshot；message_id / request_id 原样保留。

锁定的对外语义与关键不变量：
- 事件日志全量克隆：派生 messages 与父逐条一致（id/内容/顺序/created_at/metadata）
- metadata 深拷贝并排除活资源所有权键（teams/team_member/external），内容键保留
- forked_from / forked_at 追加（fork-of-fork 覆盖式，只指向直接父）
- Inbox 不继承（fork 为全新空队列）
- channel_id / workspace 沿用父；title 加 "fork of " 前缀
- external 索引零占用；父 session 不被修改
- 父不存在抛 ValueError
"""
import pytest
import pytest_asyncio
from ftre_agent.message import AssistantMsg, UserMsg
from ftre_inbox.protocol import InboundMessage
from ftre_inbox.repository import InboxRepository
from ftre_inbox.service import InboxService

from ftre.services.session.service import SessionService as SessionManager


@pytest_asyncio.fixture
async def manager(tmp_path):
    mgr = SessionManager(str(tmp_path / "sessions.db"))
    await mgr.init()
    yield mgr
    await mgr.close()


def _user(text: str):
    return UserMsg(name="default", content=text)


def _assistant(text: str):
    return AssistantMsg(name="default", content=text)


async def _seed_parent(manager, sid, n_turns=3, *, tag="seed"):
    """播种 n 轮 user/assistant 事件（request_id 以 tag 区分）。"""
    for i in range(n_turns):
        user = _user(f"user {i}")
        event = await manager.append_user_message_if_absent(
            sid,
            request_id=f"{tag}-u{i}",
            content=user.model_dump(mode="json")["content"],
            metadata=dict(user.metadata or {}),
        )
        assert event is not None
        assistant = _assistant(f"assistant {i}")
        await manager.append_event(
            sid,
            "assistant/message",
            {"message": assistant.model_dump(mode="json")},
            message_id=assistant.id,
        )


@pytest.mark.asyncio
async def test_fork_copies_snapshot_and_stays_independent(manager):
    """fork 完整拷贝 Msg Snapshot（message_id 不重生成），且与父相互独立。"""
    parent = await manager.create_session("ws", title="parent", workspace="E:\\x")
    await _seed_parent(manager, parent, n_turns=3)
    parent_msgs = await manager.get_messages_by_session(parent)

    result = await manager.fork_session(parent)
    fork_msgs = await manager.get_messages_by_session(result.fork_session_id)

    assert len(fork_msgs) == len(parent_msgs) == 6
    # 事件完整拷贝：id/内容块/顺序/created_at/metadata 与父逐条一致
    assert [m["id"] for m in fork_msgs] == [m["id"] for m in parent_msgs]
    assert [m["content"] for m in fork_msgs] == [m["content"] for m in parent_msgs]
    assert [m["role"] for m in fork_msgs] == [m["role"] for m in parent_msgs]
    assert [m["created_at"] for m in fork_msgs] == [m["created_at"] for m in parent_msgs]
    assert [m["metadata"] for m in fork_msgs] == [m["metadata"] for m in parent_msgs]

    # fork 独立：fork 上的新事件不回写父，父上的新事件不进入 fork
    await manager.append_event(
        result.fork_session_id,
        "assistant/message",
        {"message": _assistant("fork only").model_dump(mode="json")},
        message_id="msg_fork_only",
    )
    await manager.append_user_message_if_absent(
        parent,
        request_id="parent-later-u",
        content=_user("parent later").model_dump(mode="json")["content"],
        metadata={},
    )

    fork_after = await manager.get_messages_by_session(result.fork_session_id)
    parent_after = await manager.get_messages_by_session(parent)
    assert [m["content"][0]["text"] for m in fork_after].count("fork only") == 1
    assert [m["content"][0]["text"] for m in parent_after].count("fork only") == 0
    assert [m["content"][0]["text"] for m in parent_after].count("parent later") == 1
    # fork 保持 fork 时刻的快照长度（6 条）+ fork 自己的新事件（1 条），
    # 父继续增长（6 + 1）
    assert len(fork_after) == 7
    assert len(parent_after) == 7


@pytest.mark.asyncio
async def test_fork_metadata_exclude_live_resource_keys(manager):
    parent = await manager.create_session("ws", title="parent")
    await manager.update_session_metadata(parent, "plan", {"step": "keep me"})
    await manager.update_session_metadata(parent, "teams", {"t1": {"members": ["a"]}})
    await manager.update_session_metadata(parent, "team_member", {"leader": "x"})
    await manager.update_session_metadata(
        parent, "external", {"channel_id": "ws", "external_key": "k"}
    )

    result = await manager.fork_session(parent)
    fork_meta = await manager.get_session_metadata(result.fork_session_id)

    # 内容键保留
    assert fork_meta.get("plan") == {"step": "keep me"}
    # 活资源所有权键不继承
    assert "teams" not in fork_meta
    assert "team_member" not in fork_meta
    assert "external" not in fork_meta
    # 溯源字段追加
    assert fork_meta.get("forked_from") == parent
    assert fork_meta.get("forked_at")


@pytest.mark.asyncio
async def test_fork_inbox_not_inherited(manager, tmp_path):
    parent = await manager.create_session("ws")
    await _seed_parent(manager, parent, n_turns=2)
    inbox = InboxService(
        InboxRepository(tmp_path / "inbox", session_exists=manager.has_session)
    )
    await inbox.followup(InboundMessage(parent, "request-1", "ws", "queued"))
    parent_before = await inbox.snapshot(parent)
    assert len(parent_before.pending) == 1

    result = await manager.fork_session(parent)

    # fork 得到全新空 Inbox，不继承父的 pending
    fork_inbox = await inbox.snapshot(result.fork_session_id)
    assert fork_inbox.pending == ()
    assert fork_inbox.revision == 0
    assert fork_inbox.next_sequence == 1
    # 父的 pending 不受 fork 影响
    parent_after = await inbox.snapshot(parent)
    assert len(parent_after.pending) == 1
    await inbox.close()


@pytest.mark.asyncio
async def test_fork_inherits_channel_workspace_and_title(manager):
    parent = await manager.create_session("ws", title="my task", workspace="E:\\proj")
    result = await manager.fork_session(parent)

    fork = await manager.get_session(result.fork_session_id)
    assert fork["channel_id"] == "ws"
    assert fork["workspace"] == "E:\\proj"
    assert fork["title"] == "fork of my task"
    assert result.fork_session_id.startswith("ws_sess_")


@pytest.mark.asyncio
async def test_fork_title_fallback_to_session_id(manager):
    parent = await manager.create_session("ws", title="")
    result = await manager.fork_session(parent)
    fork = await manager.get_session(result.fork_session_id)
    assert fork["title"] == f"fork of {parent}"


@pytest.mark.asyncio
async def test_fork_does_not_register_external_index(manager):
    parent = await manager.create_session("ws")
    await manager.update_session_metadata(
        parent, "external", {"channel_id": "ws", "external_key": "k1"}
    )
    result = await manager.fork_session(parent)
    # fork 排除了 external，不应占用外部会话索引
    assert await manager.get_external_session(result.fork_session_id) is None


@pytest.mark.asyncio
async def test_fork_does_not_mutate_parent(manager):
    parent = await manager.create_session("ws", title="parent")
    await _seed_parent(manager, parent, n_turns=2)
    before_msgs = await manager.get_messages_by_session(parent)
    before_meta = await manager.get_session_metadata(parent)

    await manager.fork_session(parent)

    after_msgs = await manager.get_messages_by_session(parent)
    after_meta = await manager.get_session_metadata(parent)
    assert [m["id"] for m in after_msgs] == [m["id"] for m in before_msgs]
    assert after_meta.get("teams") == before_meta.get("teams")
    assert "forked_from" not in after_meta  # 父不被打上 fork 溯源


@pytest.mark.asyncio
async def test_fork_of_fork_overwrites_lineage(manager):
    parent = await manager.create_session("ws", title="root")
    fork1 = await manager.fork_session(parent)
    fork2 = await manager.fork_session(fork1.fork_session_id)

    meta2 = await manager.get_session_metadata(fork2.fork_session_id)
    # 覆盖式：只指向直接父 fork1，不保留 root 链条
    assert meta2.get("forked_from") == fork1.fork_session_id
    fork2_session = await manager.get_session(fork2.fork_session_id)
    assert fork2_session["title"] == "fork of fork of root"


@pytest.mark.asyncio
async def test_fork_missing_parent_raises(manager):
    with pytest.raises(ValueError):
        await manager.fork_session("ws_sess_nonexistent")


@pytest.mark.asyncio
async def test_fork_after_parent_deleted_raises(manager):
    parent = await manager.create_session("ws")
    await _seed_parent(manager, parent, n_turns=1)
    await manager.delete_session(parent)
    with pytest.raises(ValueError):
        await manager.fork_session(parent)


@pytest.mark.asyncio
async def test_fork_repairs_unloaded_parent_before_copying(manager):
    """未加载的旧会话 fork 时先迁移 Snapshot，不能复制悬空流式 chunk。"""
    parent = await manager.create_session("ws", title="open")
    legacy = manager.session_dir(parent) / "session.jsonl"
    legacy.write_text(
        '{"v": 1, "format": "ftre-session-log"}\n'
        '{"type":"turn/start","seq":0,"time":1,"message_id":null,"data":{"turn_id":"t1","request_id":"r1","trigger":"user"}}\n'
        '{"type":"assistant/chunk","seq":1,"time":2,"message_id":"m1","data":{"kind":"text","delta":"partial"}}\n',
        encoding="utf-8",
    )

    result = await manager.fork_session(parent)

    parent_messages = await manager.get_messages_by_session(parent)
    fork_messages = await manager.get_messages_by_session(result.fork_session_id)
    assert parent_messages[-1]["finished_reason"] == "interrupted"
    assert [message["content"] for message in fork_messages] == [
        message["content"] for message in parent_messages
    ]
    assert not legacy.exists()
