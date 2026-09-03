import asyncio
import json

import pytest
from ftre_inbox.models import QueueItem
from ftre_inbox.repository import InboxRepository


@pytest.mark.asyncio
async def test_two_targets_are_durable_and_claimed_atomically(tmp_path):
    repo = InboxRepository(tmp_path, capacity=3)
    item1 = QueueItem("r1", 0, "s1", "ws", "first")
    item2 = QueueItem("r2", 0, "s1", "ws", "steer", source="user")

    assert (await repo.admit(item1, "next-turn"))[0] is True
    assert (await repo.admit(item2, "next-step"))[0] is True
    snap = await repo.snapshot("s1")
    assert [item.request_id for item in snap.next_turn] == ["r1"]
    assert [item.request_id for item in snap.next_step] == ["r2"]

    assert await repo.claim("s1", ("r2", "r1")) == (snap.next_step[0], snap.next_turn[0])
    assert (await repo.snapshot("s1")).has_pending is False


@pytest.mark.asyncio
async def test_claim_is_all_or_nothing(tmp_path):
    repo = InboxRepository(tmp_path)
    await repo.admit(QueueItem("r1", 0, "s1", "ws", "first"), "next-turn")
    assert await repo.claim("s1", ("r1", "missing")) == ()
    assert (await repo.snapshot("s1")).next_turn[0].request_id == "r1"


@pytest.mark.asyncio
async def test_admission_is_idempotent_against_committed_history(tmp_path):
    seen = {("s1", "already")}
    repo = InboxRepository(tmp_path, request_seen=lambda session, request: (session, request) in seen)
    created, _ = await repo.admit(QueueItem("already", 0, "s1", "ws", "duplicate"), "next-turn")
    assert created is False
    assert not (await repo.snapshot("s1")).has_pending


@pytest.mark.asyncio
async def test_claim_and_remove_compete_without_double_success(tmp_path):
    repo = InboxRepository(tmp_path)
    await repo.admit(QueueItem("r1", 0, "s1", "ws", "first"), "next-turn")
    claimed, removed = await asyncio.gather(
        repo.claim("s1", ("r1",)),
        repo.remove("s1", "r1"),
    )
    assert bool(claimed) ^ bool(removed)
    assert not (await repo.snapshot("s1")).has_pending


@pytest.mark.asyncio
async def test_snapshot_survives_repository_recreation(tmp_path):
    repo = InboxRepository(tmp_path)
    await repo.admit(QueueItem("r1", 0, "s1", "ws", "first"), "next-turn")
    replacement = InboxRepository(tmp_path)
    await replacement.load_all()
    snapshot = await replacement.snapshot("s1")
    assert snapshot.next_turn[0].content == "first"


@pytest.mark.asyncio
async def test_repository_migrates_legacy_run_binding_from_disk(tmp_path):
    path = tmp_path / "s1" / "inbox.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "session_id": "s1",
                "revision": 1,
                "next_sequence": 2,
                "next_turn": [],
                "next_step": [
                    {
                        "request_id": "r1",
                        "sequence": 1,
                        "session_id": "s1",
                        "channel_id": "ws",
                        "content": "继续",
                        "source": "user",
                        "messages": [],
                        "agent_id": "default",
                        "target_run_id": "old-run",
                        "target": "next-step",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    repo = InboxRepository(tmp_path)
    await repo.load_all()

    assert (await repo.snapshot("s1")).next_step[0].request_id == "r1"
    assert "target_run_id" not in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_legacy_mailbox_is_migrated_once(tmp_path):
    legacy = tmp_path / "sessions"
    state_dir = legacy / "s1"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        '{"schema_version": 1, "session": {"id": "s1", "channel_id": "ws"}, '
        '"messages": [], "mailbox": {"revision": 2, "next_sequence": 2, '
        '"pending": [{"request_id": "old", "sequence": 1, "content": "legacy", '
        '"attachments": [], "agent_id": "default"}]}, "metadata": {}}',
        encoding="utf-8",
    )
    repo = InboxRepository(legacy / "_inbox", legacy_root=legacy)
    await repo.load_all()
    assert (await repo.snapshot("s1")).next_turn[0].request_id == "old"
    assert "mailbox" not in (state_dir / "state.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_empty_legacy_mailbox_is_removed_without_creating_pending(tmp_path):
    legacy = tmp_path / "sessions"
    state_dir = legacy / "s1"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        '{"schema_version": 1, "session": {"id": "s1", "channel_id": "ws"}, '
        '"messages": [], "mailbox": {"revision": 4, "next_sequence": 5, "pending": []}, '
        '"metadata": {}}',
        encoding="utf-8",
    )
    repo = InboxRepository(legacy / "_inbox", legacy_root=legacy)
    await repo.load_all()
    assert not (await repo.snapshot("s1")).has_pending
    assert "mailbox" not in (state_dir / "state.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_duplicate_legacy_request_is_quarantined_for_diagnosis(tmp_path):
    legacy = tmp_path / "sessions"
    state_dir = legacy / "s1"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        '{"schema_version": 1, "session": {"id": "s1", "channel_id": "ws"}, '
        '"messages": [], "mailbox": {"pending": ['
        '{"request_id": "same", "sequence": 1}, {"request_id": "same", "sequence": 2}]}, '
        '"metadata": {}}',
        encoding="utf-8",
    )
    repo = InboxRepository(legacy / "_inbox", legacy_root=legacy)
    await repo.load_all()
    # 迁移失败不删除旧事实，也不伪造一份空 Inbox。
    assert "mailbox" in (state_dir / "state.json").read_text(encoding="utf-8")
