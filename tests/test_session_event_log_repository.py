"""Session Event 内存提交与 Msg Snapshot 持久化不变量（PRD-F44）。"""
import asyncio

import pytest
import pytest_asyncio
from ftre_agent.message import AssistantMsg, UserMsg

from ftre.services.session.service import SessionService


@pytest_asyncio.fixture
async def manager(tmp_path):
    mgr = SessionService(str(tmp_path / "sessions.db"), snapshot_interval_ms=20)
    await mgr.init()
    yield mgr
    await mgr.close()


@pytest.mark.asyncio
async def test_concurrent_append_event_loses_nothing_and_snapshot_is_compact(manager):
    sid = await manager.create_session("ws")
    await manager.get_messages_by_session(sid)
    messages = [AssistantMsg(name="default", content=f"m{i}") for i in range(20)]
    await asyncio.gather(
        *(
            manager.append_event(
                sid,
                "assistant/message",
                {"message": message.model_dump(mode="json")},
                message_id=message.id,
            )
            for message in messages
        )
    )
    log = await manager.log(sid)
    assert [event["seq"] for event in log.events] == list(range(20))
    derived = await manager.get_messages_by_session(sid)
    assert [message["id"] for message in derived] == [message.id for message in messages]
    await manager.flush_log(sid)
    directory = manager.session_dir(sid)
    assert (directory / "session.json").exists()
    assert not (directory / "session.jsonl").exists()
    payload = (directory / "session.json").read_text(encoding="utf-8")
    assert payload.count('"messages"') == 1


@pytest.mark.asyncio
async def test_snapshot_seq_and_msg_seq_are_explicit(manager):
    sid = await manager.create_session("ws")
    await manager.append_event(
        sid,
        "assistant/message",
        {"message": AssistantMsg(content="x", id="m").model_dump(mode="json")},
        message_id="m",
    )
    await manager.flush_log(sid)
    assert await manager.get_session(sid) is not None
    messages, _, seq = await manager.get_messages_snapshot(sid)
    assert seq == 0
    assert messages[0]["id"] == "m"
    assert messages[0]["seq"] == 0


@pytest.mark.asyncio
async def test_multiple_chunks_advance_one_msg_seq(manager):
    sid = await manager.create_session("ws")
    for delta in ("a", "b", "c"):
        await manager.append_event(
            sid,
            "assistant/chunk",
            {"kind": "text", "delta": delta, "block_id": "text-1"},
            message_id="assistant-1",
        )

    messages, _, seq = await manager.get_messages_snapshot(sid)
    assert seq == 2
    assert len(messages) == 1
    assert messages[0]["id"] == "assistant-1"
    assert messages[0]["seq"] == 2
    assert messages[0]["content"][0]["text"] == "abc"


@pytest.mark.asyncio
async def test_snapshot_seq_matches_the_msg_cut_captured_before_concurrent_append(manager):
    sid = await manager.create_session("ws")
    await manager.append_event(
        sid,
        "assistant/chunk",
        {"kind": "text", "delta": "a", "block_id": "text-1"},
        message_id="assistant-1",
    )

    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    committed: list[tuple[int, str]] = []
    original_commit = manager._repo.commit

    async def delayed_commit(state):
        assistant = next(
            (message for message in state.messages if message.get("id") == "assistant-1"),
            None,
        )
        committed.append((int(state.seq), str((assistant or {}).get("content", [{}])[0].get("text", ""))))
        if len(committed) == 1:
            commit_started.set()
            await release_commit.wait()
        await original_commit(state)

    manager._repo.commit = delayed_commit
    flush_task = asyncio.create_task(manager.flush_log(sid))
    await commit_started.wait()
    await manager.append_event(
        sid,
        "assistant/chunk",
        {"kind": "text", "delta": "b", "block_id": "text-1"},
        message_id="assistant-1",
    )
    release_commit.set()
    await flush_task

    assert committed[0] == (0, "a")
    assert committed[-1] == (1, "ab")


@pytest.mark.asyncio
async def test_restart_continues_session_seq_from_snapshot(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    first = SessionService(db_path, snapshot_interval_ms=20)
    await first.init()
    sid = await first.create_session("ws")
    await first.append_event(
        sid,
        "assistant/chunk",
        {"kind": "text", "delta": "before", "block_id": "text-1"},
        message_id="assistant-1",
    )
    await first.flush_log(sid)
    await first.close()

    second = SessionService(db_path, snapshot_interval_ms=20)
    await second.init()
    try:
        event = await second.append_event(
            sid,
            "assistant/chunk",
            {"kind": "text", "delta": "after", "block_id": "text-1"},
            message_id="assistant-1",
        )
        assert event["seq"] == 1
        pending, has_more, resync, current = await second.events_after(
            sid, after_seq=0
        )
        assert has_more is False
        assert resync is False
        assert current == 1
        assert [item["seq"] for item in pending] == [1]
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_restart_reads_snapshot_for_messages_and_context(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    first = SessionService(db_path, snapshot_interval_ms=20)
    await first.init()
    sid = await first.create_session("ws")
    user = UserMsg(content="persisted question", metadata={"hide": False})
    await first.append_user_message_if_absent(
        sid,
        request_id="persisted-request",
        content=user.model_dump(mode="json")["content"],
        metadata=dict(user.metadata or {}),
    )
    assistant = AssistantMsg(content="persisted answer", id="persisted-assistant")
    await first.append_event(
        sid,
        "assistant/message",
        {"message": assistant.model_dump(mode="json")},
        message_id=assistant.id,
    )
    await first.flush_log(sid)
    await first.close()

    second = SessionService(db_path, snapshot_interval_ms=20)
    await second.init()
    try:
        messages, has_more, _ = await second.get_messages_snapshot(sid)
        assert has_more is False
        assert [message["content"][0]["text"] for message in messages] == [
            "persisted question",
            "persisted answer",
        ]
        context = await second.get_context_messages(sid)
        assert [message["content"][0]["text"] for message in context] == [
            "persisted question",
            "persisted answer",
        ]
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_restart_request_id_is_idempotent_and_rejects_content_conflict(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    first = SessionService(db_path, snapshot_interval_ms=20)
    await first.init()
    sid = await first.create_session("ws")
    original = UserMsg(content="same request", metadata={"hide": False})
    original_content = original.model_dump(mode="json")["content"]
    assert await first.append_user_message_if_absent(
        sid,
        request_id="stable-request",
        content=original_content,
        metadata=dict(original.metadata or {}),
    )
    await first.flush_log(sid)
    await first.close()

    second = SessionService(db_path, snapshot_interval_ms=20)
    await second.init()
    try:
        assert await second.append_user_message_if_absent(
            sid,
            request_id="stable-request",
            content=original_content,
        ) is None
        conflicting = UserMsg(content="different request", metadata={"hide": False})
        with pytest.raises(ValueError, match="request_id 已绑定不同内容"):
            await second.append_user_message_if_absent(
                sid,
                request_id="stable-request",
                content=conflicting.model_dump(mode="json")["content"],
            )
    finally:
        await second.close()
