"""SessionLane 的架构级测试：pending 持久化、内存运行态与精确等待。"""
from __future__ import annotations

import asyncio
import json

import pytest

from ftre.services.agent.runtime.loop.completion_registry import CompletionRegistry
from ftre.services.agent.runtime.loop.context_gate import ContextDecision
from ftre.services.agent.runtime.mailbox.store import MailboxStore
from ftre.services.agent.runtime.mailbox.lane import SessionLane, SessionLaneRegistry
from ftre.services.agent.runtime.loop.turn_executor import TurnOutcome
from ftre.services.messaging.bus import BusMessage
from ftre.services.session.service import SessionService as SessionManager


class _PassGate:
    async def before_claim(self, *args):
        return ContextDecision("pass")

    async def after_turn(self, *args):
        return ContextDecision("pass")

    async def compact(self, *args):
        return ContextDecision("pass")


class _Executor:
    def __init__(self, *, pause: asyncio.Event | None = None) -> None:
        self.seen: list[str] = []
        self.seen_channels: list[str] = []
        self.seen_metadata: list[dict] = []
        self.started = asyncio.Event()
        self.pause = pause

    async def resolve_inbound_config(self, inbound, *, turn_id):
        return object(), None

    async def execute(
        self,
        inbound,
        *,
        turn_id,
        config,
        agent_profile,
    ):
        self.seen.append(inbound.data["content"])
        self.seen_channels.append(inbound.from_channel)
        self.started.set()
        if self.pause is not None:
            await self.pause.wait()
        return TurnOutcome(
            turn_id=turn_id,
            status="completed",
            user_message_id=f"user_{turn_id}",
            final_content=inbound.data["content"],
        )


def _inbound(session_id: str, content: str, request_id: str) -> BusMessage:
    return BusMessage(
        type="user_message",
        from_channel="test",
        from_session=session_id,
        to_channel="test",
        to_session=session_id,
        data={
            "session_id": session_id,
            "content": content,
        },
        metadata={"request_id": request_id},
    )


async def _lane(
    tmp_path, executor: _Executor, *, capacity: int = 100
) -> tuple[SessionLane, CompletionRegistry, str]:
    sessions = SessionManager(sessions_dir=str(tmp_path / "sessions"))
    await sessions.init()
    session_id = await sessions.create_session("test")
    mailbox = MailboxStore(sessions, capacity=capacity)
    completion = CompletionRegistry()
    lane = SessionLane(
        session_id,
        mailbox=mailbox,
        context_gate=_PassGate(),
        executor=executor,
        completion=completion,
        publish_snapshot=lambda _sid: asyncio.sleep(0),
    )
    return lane, completion, session_id


@pytest.mark.asyncio
async def test_same_session_is_fifo_and_waits_by_request_id(tmp_path):
    executor = _Executor()
    lane, completion, session_id = await _lane(tmp_path, executor)

    first = await lane.submit(_inbound(session_id, "A", "client-A"))
    second = await lane.submit(_inbound(session_id, "B", "client-B"))
    done_a, done_b = await asyncio.gather(
        completion.wait(session_id, first.request_id),
        completion.wait(session_id, second.request_id),
    )

    assert executor.seen == ["A", "B"]
    assert done_a.status == "completed"
    assert done_b.final_content == "B"
    await lane.close()


@pytest.mark.asyncio
async def test_admission_snapshot_is_published_before_worker_can_claim_head(tmp_path):
    """客户端先看到 pending，再允许 worker 领取，避免旧快照倒灌 UI。"""
    executor = _Executor()
    sessions = SessionManager(sessions_dir=str(tmp_path / "sessions"))
    await sessions.init()
    session_id = await sessions.create_session("test")
    mailbox = MailboxStore(sessions)
    snapshots: list[list[str]] = []

    async def record_snapshot(_session_id: str) -> None:
        state = await mailbox.snapshot(session_id)
        snapshots.append([item.content for item in state.pending])

    lane = SessionLane(
        session_id,
        mailbox=mailbox,
        context_gate=_PassGate(),
        executor=executor,
        completion=CompletionRegistry(),
        publish_snapshot=record_snapshot,
    )

    await lane.submit(_inbound(session_id, "A", "client-A"))

    # submit 返回前已经发出了入队快照；此时 worker 尚未有机会拿走 A。
    assert snapshots[0] == ["A"]
    await lane.close()


@pytest.mark.asyncio
async def test_execution_channel_is_read_from_session_not_queue_item(tmp_path):
    """QueueItem 不存路由；执行时统一从目标 session 读取 Channel。"""
    executor = _Executor()
    lane, completion, session_id = await _lane(tmp_path, executor)
    inbound = _inbound(session_id, "来自 cron 的唤起", "client-invoke")
    accepted = await lane.submit(inbound)
    outcome = await completion.wait(session_id, accepted.request_id)

    assert outcome.status == "completed"
    assert executor.seen_channels == ["test"]
    await lane.close()


@pytest.mark.asyncio
async def test_cancel_pending_keeps_active_turn_and_completes_waiter_in_memory(tmp_path):
    release = asyncio.Event()
    executor = _Executor(pause=release)
    lane, completion, session_id = await _lane(tmp_path, executor)

    first = await lane.submit(_inbound(session_id, "A", "client-A"))
    await executor.started.wait()
    second = await lane.submit(_inbound(session_id, "B", "client-B"))
    cancelled = await lane.cancel_pending(second.request_id)
    assert cancelled is not None and cancelled.request_id == second.request_id

    release.set()
    done_a, done_b = await asyncio.gather(
        completion.wait(session_id, first.request_id),
        completion.wait(session_id, second.request_id),
    )
    assert done_a.status == "completed"
    assert done_b.status == "cancelled"
    assert executor.seen == ["A"]
    await lane.close()


@pytest.mark.asyncio
async def test_cancel_active_matches_request_and_then_drains_next_pending(tmp_path):
    """控制面只取消指定 active 请求，后续 FIFO 消息仍会继续执行。"""
    release = asyncio.Event()
    executor = _Executor(pause=release)
    lane, completion, session_id = await _lane(tmp_path, executor)

    first = await lane.submit(_inbound(session_id, "A", "request-A"))
    await executor.started.wait()
    second = await lane.submit(_inbound(session_id, "B", "request-B"))

    assert await lane.cancel_active("request-other") is False
    assert await lane.cancel_active(first.request_id) is True
    done_a = await completion.wait(session_id, first.request_id)
    assert done_a.status == "cancelled"

    # A 被取消后，Lane 不退出；B 仍由同一个 worker 领取并执行。
    release.set()
    done_b = await completion.wait(session_id, second.request_id)
    assert done_b.status == "completed"
    assert executor.seen == ["A", "B"]
    await lane.close()


@pytest.mark.asyncio
async def test_capacity_only_counts_persisted_pending_requests(tmp_path):
    release = asyncio.Event()
    executor = _Executor(pause=release)
    lane, _completion, session_id = await _lane(tmp_path, executor, capacity=1)

    accepted = await lane.submit(_inbound(session_id, "A", "client-A"))
    assert accepted.accepted
    await executor.started.wait()
    queued = await lane.submit(_inbound(session_id, "B", "client-B"))
    assert queued.accepted
    rejected = await lane.submit(_inbound(session_id, "C", "client-C"))
    assert rejected.accepted is False
    assert rejected.error["code"] == "queue_full"

    release.set()
    await lane.close()


@pytest.mark.asyncio
async def test_running_turn_never_enters_state_json(tmp_path):
    """领取后的 A 只在 Lane 内存；磁盘只保留仍待执行的 B。"""
    release = asyncio.Event()
    executor = _Executor(pause=release)
    lane, _completion, session_id = await _lane(tmp_path, executor)

    await lane.submit(_inbound(session_id, "A", "client-A"))
    await executor.started.wait()
    await lane.submit(_inbound(session_id, "B", "client-B"))

    payload = json.loads((tmp_path / "sessions" / session_id / "state.json").read_text("utf-8"))
    assert set(payload["mailbox"]) == {"revision", "next_sequence", "pending"}
    assert [item["content"] for item in payload["mailbox"]["pending"]] == ["B"]
    assert set(payload["mailbox"]["pending"][0]) == {
        "request_id", "sequence", "content", "attachments", "agent_id",
    }

    release.set()
    await lane.close()


@pytest.mark.asyncio
async def test_turn_completion_advances_snapshot_revision_even_without_pending_change(tmp_path):
    """Turn 完成后 pending 已为空，客户端仍必须收到更新后的 idle 快照。"""
    release = asyncio.Event()
    executor = _Executor(pause=release)
    sessions = SessionManager(sessions_dir=str(tmp_path / "sessions"))
    await sessions.init()
    session_id = await sessions.create_session("test")
    mailbox = MailboxStore(sessions)
    completion = CompletionRegistry()
    lane = SessionLane(
        session_id,
        mailbox=mailbox,
        context_gate=_PassGate(),
        executor=executor,
        completion=completion,
        publish_snapshot=lambda _sid: asyncio.sleep(0),
    )

    accepted = await lane.submit(_inbound(session_id, "A", "client-A"))
    await executor.started.wait()
    revision_while_running = (await mailbox.snapshot(session_id)).revision

    release.set()
    await completion.wait(session_id, accepted.request_id)
    revision_after_completion = (await mailbox.snapshot(session_id)).revision

    assert revision_after_completion > revision_while_running
    await lane.close()


@pytest.mark.asyncio
async def test_claimed_item_only_lives_in_memory_until_turn_finishes(tmp_path):
    """领取后没有第二份 active 队列；只由 TurnOperation 保存当前请求。"""
    release = asyncio.Event()

    class _HandoffExecutor(_Executor):
        async def execute(self, inbound, *, turn_id, config, agent_profile):
            self.seen.append(inbound.data["content"])
            self.started.set()
            await release.wait()
            return TurnOutcome(turn_id=turn_id, status="completed")

    executor = _HandoffExecutor()
    lane, _completion, session_id = await _lane(tmp_path, executor)

    await lane.submit(_inbound(session_id, "A", "client-A"))
    await executor.started.wait()
    operation = lane.operation
    assert operation is not None
    assert operation.item.content == "A"
    assert (await lane.snapshot()).pending == []
    release.set()
    await lane.close()


@pytest.mark.asyncio
async def test_close_fence_rejects_submit_instead_of_creating_a_new_lane(tmp_path):
    """删除会话的 close 与 submit 并发时，后到消息必须明确拒绝。"""
    sessions = SessionManager(sessions_dir=str(tmp_path / "sessions"))
    await sessions.init()
    session_id = await sessions.create_session("test")
    mailbox = MailboxStore(sessions)
    registry = SessionLaneRegistry(
        mailbox=mailbox,
        context_gate=_PassGate(),
        executor=_Executor(),
        completion=CompletionRegistry(),
        publish_snapshot=lambda _sid: asyncio.sleep(0),
    )

    await registry.close_session(session_id)
    rejected = await registry.submit(_inbound(session_id, "不能重新入队", "client-late"))

    assert rejected.accepted is False
    assert rejected.error["code"] == "session_closing"


@pytest.mark.asyncio
async def test_after_turn_compacts_even_when_queue_is_empty(tmp_path):
    """turn 结束后队列已空也必须执行预压缩水位检查。

    回归锁定：此前 after_turn 只在 peek() 非空（还有等待消息）时检查，导致
    空闲会话的压缩被推迟到下一条消息 before_claim 才发生——客户端气泡
    「压缩中」直到用户再发消息才出现，新消息被迫排队等压缩。
    """
    compact_calls: list[str] = []
    after_turn_calls = 0

    class _CompactGate(_PassGate):
        async def after_turn(self, *args):
            nonlocal after_turn_calls
            after_turn_calls += 1
            return ContextDecision("compact", "本轮结束后达到预压缩水位")

        async def compact(self, session_id, *args):
            compact_calls.append(session_id)
            return ContextDecision("pass")

    sessions = SessionManager(sessions_dir=str(tmp_path / "sessions"))
    await sessions.init()
    session_id = await sessions.create_session("test")
    mailbox = MailboxStore(sessions)
    lane = SessionLane(
        session_id,
        mailbox=mailbox,
        context_gate=_CompactGate(),
        executor=_Executor(),
        completion=CompletionRegistry(),
        publish_snapshot=lambda _sid: asyncio.sleep(0),
    )

    # 单条消息：turn 完成时队列必然已空。
    await lane.submit(_inbound(session_id, "A", "client-A"))
    # 等 worker 自然退出（队列空 → after_turn 检查 → peek None → return）。
    # 不能用 close()：close 会 cancel worker，收尾路径（含 after_turn）不会执行。
    worker = lane._worker
    if worker is not None:
        await worker
    await lane.close()

    assert after_turn_calls == 1
    assert compact_calls == [session_id]
