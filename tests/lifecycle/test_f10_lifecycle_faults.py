"""F12 Inbox 生命周期、取消、Hook 失败和恢复契约。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from ftre_inbox.repository import InboxRepository
from ftre_inbox.service import InboxService

from ftre.app.gateway.composition import build_composition
from ftre.services.agent.contracts import InboundMessage
from ftre.services.agent.runtime.engine import AgentLoop


class _Agent:
    def __init__(self) -> None:
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    def is_busy(self, _session_id: str) -> bool:
        return bool(self.calls and not self.release.is_set())

    async def run(self, message: InboundMessage):
        self.calls.append(message.request_id)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return message.request_id

    async def cancel(self, _session_id: str):
        return False


@pytest.mark.asyncio
async def test_close_cancels_worker_and_keeps_claimed_state_out_of_pending(tmp_path):
    agent = _Agent()
    repo = InboxRepository(tmp_path / "inbox")
    service = InboxService(repo, agent)
    await service.followup(InboundMessage("s1", "r1", "ws", "hello"))
    await agent.started.wait()
    await service.close()
    assert agent.cancelled is True
    # r1 已经 claim，关闭不回滚为 pending；未 claim 的数据仍可恢复。
    assert (await repo.snapshot("s1")).pending == ()


@pytest.mark.asyncio
async def test_before_claim_keep_preserves_pending_and_remove_is_explicit(tmp_path):
    repo = InboxRepository(tmp_path / "inbox")
    service = InboxService(repo)
    await service.followup(InboundMessage("s1", "r1", "ws", "hello"))

    async def keep(_item, _snapshot):
        return False

    service.bind_before_claim(keep)
    # 没有 Agent 时不会启动 worker，直接验证 pending 仍可被 remove。
    assert (await service.snapshot("s1")).pending[0].request_id == "r1"
    assert await service.remove("s1", "r1") is True
    assert not (await service.snapshot("s1")).has_pending
    await service.close()


@pytest.mark.asyncio
async def test_restart_recovers_unclaimed_item_without_duplicate(tmp_path):
    root = tmp_path / "inbox"
    first = InboxService(InboxRepository(root))
    await first.followup(InboundMessage("s1", "r1", "ws", "hello"))
    await first.close()

    second_repo = InboxRepository(root)
    await second_repo.load_all()
    snapshot = await second_repo.snapshot("s1")
    assert [item.request_id for item in snapshot.pending] == ["r1"]
    await second_repo.load_all()
    assert [item.request_id for item in (await second_repo.snapshot("s1")).pending] == ["r1"]


@pytest.mark.asyncio
async def test_inbox_plugin_restart_replaces_closed_service_without_duplicate_listener():
    composition = await build_composition({})
    try:
        first = composition.context.get("inbox")
        assert first is not None and first._closed is False
        assert composition.context.get("agents").driver is not None
        assert await composition.plugins.restart("inbox") is True
        second = composition.context.get("inbox")
        assert second is not first
        assert second._closed is False
        assert composition.context.get("channels").manager.get("ws")._current_inbox() is second
        entries = composition.context.get("hook_runtime").snapshot("session/disposed")
        assert len([entry for entry in entries if not entry.disposed]) == 1
    finally:
        await composition.close()


@pytest.mark.asyncio
async def test_inbox_plugin_unload_releases_worker_state_and_listener():
    composition = await build_composition({})
    service = composition.context.get("inbox")
    try:
        assert await composition.plugins.unload("inbox") is True
        assert service._closed is True
        assert service.repository._states == {}
        assert composition.context.get("agent_runtime", strict=False) is None
        assert not [
            entry
            for entry in composition.context.get("hook_runtime").snapshot("session/disposed")
            if not entry.disposed
        ]
    finally:
        await composition.close()


@pytest.mark.asyncio
async def test_delete_session_waits_for_active_turn_before_removing_history():
    """删除执行中 Session 时，必须先等 Turn 的取消收尾完成。"""
    loop = object.__new__(AgentLoop)
    loop.session_manager = AsyncMock()
    loop.session_manager.get_session_metadata.return_value = {}
    order: list[str] = []

    async def delete_history(_session_id: str) -> None:
        order.append("delete-history")

    loop.session_manager.delete_session.side_effect = delete_history
    started = asyncio.Event()
    child_finished = asyncio.Event()
    parent_finished = asyncio.Event()

    async def active_turn():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            order.append("turn-cancelled")
            # 模拟 TurnExecutor 的最后一段消息/Hooks 收尾。
            await asyncio.sleep(0)
            order.append("turn-finished")
            child_finished.set()

    async def parent_cleanup():
        await child_finished.wait()
        await asyncio.sleep(0)
        order.append("parent-finished")
        parent_finished.set()

    task = asyncio.create_task(active_turn())
    await started.wait()
    loop._direct_tasks = {"s1": task}
    loop._direct_signals = {"s1": asyncio.Event()}
    loop._direct_completion_events = {"s1": parent_finished}
    loop._direct_parent_tasks = {"s1": None}
    cleanup = asyncio.create_task(parent_cleanup())

    await loop.delete_session("s1")
    await cleanup

    assert task.done()
    assert order == [
        "turn-cancelled", "turn-finished", "parent-finished", "delete-history",
    ]


@pytest.mark.asyncio
async def test_deleted_session_does_not_publish_status_to_empty_channel():
    """Turn finally 晚于 Session 删除时，不向空通道发送伪状态。"""
    loop = object.__new__(AgentLoop)
    loop.session_manager = AsyncMock()
    loop.session_manager.get_session.return_value = None
    loop.bus = AsyncMock()

    await loop._publish_session_status_async("deleted", "idle")

    loop.bus.publish_outbound.assert_not_awaited()
