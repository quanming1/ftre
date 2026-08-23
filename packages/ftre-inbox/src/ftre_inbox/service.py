"""Inbox Service 和按 Session 串行的 worker。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ftre.services.messaging.bus import BusMessage

from ftre.services.messaging.bus import IngressResult

from .hooks import (
    INBOX_BEFORE_CLAIM_SPEC,
    INBOX_CHANGED_SPEC,
    INBOX_CLAIMED_SPEC,
    INBOX_DISCARDED_SPEC,
    INBOX_INSERTED_SPEC,
    INBOX_STATUS_CHANGED_SPEC,
    BeforeClaimPayload,
    EnterClaim,
    InboxChangedPayload,
    InboxMutationPayload,
    InboxStatusPayload,
    RejectClaim,
)
from .models import InboxSnapshot, QueueItem, QueueTarget
from .protocol import InboundMessage
from .repository import InboxRepository

logger = logging.getLogger(__name__)


def _content_text(value: Any) -> str:
    """把线协议的字符串/文本 parts 归一成 Agent 输入字符串。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [
            str(part.get("text", ""))
            for part in value
            if isinstance(part, dict) and part.get("type", "text") == "text"
        ]
        return "".join(parts)
    return str(value or "")


class InboxService:
    """独立队列能力。

    ``agent`` 只需要实现 ``run(InboundMessage)``、``is_busy`` 和 ``cancel``；本类不把
    QueueItem 传进 AgentService，因此 Agent 运行时不会被队列模型污染。
    """

    key = "inbox"
    # Protocol providers read these public Hook contracts through the Service
    # instance; the Gateway core never imports the optional package directly.
    changed_hook_spec = INBOX_CHANGED_SPEC
    status_hook_spec = INBOX_STATUS_CHANGED_SPEC

    @property
    def is_closed(self) -> bool:
        """Expose lifecycle state without leaking the private flag to Providers."""
        return self._closed

    def __init__(
        self,
        repository: InboxRepository,
        agent=None,
        *,
        hook_runtime=None,
        before_claim=None,
    ) -> None:
        self.repository = repository
        self._agent = agent
        self._workers: dict[str, asyncio.Task] = {}
        self._wake: dict[str, asyncio.Event] = {}
        self._receipts: dict[tuple[str, str], asyncio.Future] = {}
        self._closed = False
        self._before_claim = before_claim
        self._hook_runtime = hook_runtime
        self._blocked: dict[str, str] = {}

    async def start(self) -> None:
        await self.repository.load_all()
        for session_id in self.repository.recoverable_sessions():
            self._ensure_worker(session_id)

    async def close(self) -> None:
        """停止 worker 并解除所有宿主回调，保留磁盘 pending 供下次恢复。"""
        self._closed = True
        workers = tuple(self._workers.values())
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._wake.clear()
        self._blocked.clear()
        self.repository.close()
        for future in self._receipts.values():
            if not future.done():
                future.cancel()
        self._receipts.clear()
        # Hook Runtime 和 Agent 都是本实例构造时注入的依赖；关闭时解除引用，
        # 避免旧 Fiber/Task 被队列实例继续保活。
        self._before_claim = None
        self._hook_runtime = None
        self._agent = None

    async def followup(self, message: InboundMessage) -> IngressResult:
        return await self._admit(message, "next-turn")

    async def steer(self, message: InboundMessage) -> IngressResult:
        return await self._admit(message, "next-step")

    async def inject(self, message: InboundMessage) -> IngressResult:
        return await self._admit(message, "next-step", wake=False)

    async def snapshot(self, session_id: str) -> InboxSnapshot:
        return await self.repository.snapshot(session_id)

    async def delete_session(self, session_id: str) -> None:
        """删除会话时一并清理 Inbox；不触碰宿主 Session 历史。"""
        task = self._workers.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._wake.pop(session_id, None)
        self._blocked.pop(session_id, None)
        for key, future in tuple(self._receipts.items()):
            if key[0] == session_id:
                if not future.done():
                    future.cancel()
                self._receipts.pop(key, None)
        await self.repository.delete_session(session_id)

    async def handle_bus_message(self, message: BusMessage) -> IngressResult:
        """把通用 inbound 信封转换成 InboundMessage，再选择投递语义。"""
        session_id = str(message.data.get("session_id") or message.from_session)
        if message.type == "turn_cancel":
            cancelled = await self.cancel(
                session_id,
                str(message.data.get("request_id") or "") or None,
            )
            return IngressResult(
                accepted=True,
                session_id=session_id,
                request_id=str(message.metadata.request_id or ""),
                created=cancelled,
            )
        inbound = InboundMessage(
            session_id=session_id,
            request_id=str(message.metadata.request_id or message.id),
            channel_id=str(message.from_channel),
            content=_content_text(message.data.get("content")),
            attachments=tuple(dict(item) for item in (message.data.get("attachments") or ())),
            source=str(message.data.get("source") or "user"),
            metadata=message.metadata.model_dump(mode="json"),
        )
        mode = str(message.data.get("mode") or "queue")
        if mode == "steer":
            return await self.steer(inbound)
        return await self.followup(inbound)

    async def wire_snapshot(self, session_id: str) -> dict[str, Any]:
        """返回不泄漏 next-turn/next-step 的客户端视图。"""
        snapshot = await self.repository.snapshot(session_id)
        next_turn_ids = {item.request_id for item in snapshot.next_turn}
        items = [
            {
                "id": item.request_id,
                "placement": (
                    "queued"
                    if item.request_id in next_turn_ids
                    else ("steering" if item.source == "user" else "context")
                ),
                "message": {
                    "content": [{"type": "text", "text": item.content}],
                    "attachments": [dict(value) for value in item.attachments],
                },
            }
            for item in snapshot.pending
        ]
        return {"session_id": session_id, "items": items}

    async def edit(self, session_id: str, request_id: str, content: str, attachments=None) -> bool:
        result = await self.repository.edit(session_id, request_id, content, attachments)
        if result is None:
            return False
        await self._publish(session_id)
        self._wake_event(session_id).set()
        return True

    async def remove(self, session_id: str, request_id: str) -> bool:
        before = await self.repository.snapshot(session_id)
        result = await self.repository.remove(session_id, request_id)
        if result is None:
            return False
        await self._publish(session_id)
        self._wake_event(session_id).set()
        await self._emit_mutation(
            INBOX_DISCARDED_SPEC,
            InboxMutationPayload(
                session_id=session_id,
                item=result,
                target=("next-step" if result in before.next_step else "next-turn"),
                operation="discarded",
            ),
        )
        return True

    async def promote(self, session_id: str, request_id: str) -> bool:
        result = await self.repository.promote(session_id, request_id)
        if result is None:
            return False
        await self._publish(session_id)
        self._wake_event(session_id).set()
        return True

    async def cancel(self, session_id: str, request_id: str | None = None) -> bool:
        """取消 active；给出 request_id 时只移除 pending。"""
        if request_id:
            return await self.remove(session_id, request_id)
        if self._agent is None:
            return False
        result = self._agent.cancel(session_id)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    async def wait(self, session_id: str, request_id: str):
        """等待本进程内 ``followup`` 交付的 Turn 结果。

        ``steer``/``inject`` 可能在 active Turn 的 Core Hook 中直接变成上下文，
        它们没有独立的 TurnOutcome；因此不创建永不完成的 receipt，调用方应使用
        Session/Turn 状态或 ``wait_session_quiescent`` 观察整体完成。
        """
        key = (session_id, request_id)
        future = self._receipts.get(key)
        if future is None:
            raise ValueError(
                "只有 followup/next-turn 输入提供可等待的 Turn receipt"
            )
        return await future

    async def wait_session_quiescent(self, session_id: str):
        """等待 Inbox pending 和 Agent active Turn 都清空。"""
        while True:
            snapshot = await self.repository.snapshot(session_id)
            busy = self._agent is not None and self._agent.is_busy(session_id)
            if not snapshot.has_pending and not busy:
                return {"session_id": session_id, "status": "quiescent"}
            await asyncio.sleep(0.02)

    def notify_agent_idle(self, session_id: str) -> None:
        """由 AgentService 在 active Turn 收尾后唤醒等待中的 Inbox worker。"""
        self._wake_event(session_id).set()

    async def claim_next_step_for_reasoning(
        self,
        session_id: str,
    ) -> tuple[QueueItem, ...]:
        """原子领取当前 active Turn 的 ``next-step`` 消息。

        该入口只由 ``agent/before-reasoning`` Hook 调用。它复用同一套
        ``peek → before-claim → claim`` 逻辑，但不会启动第二个 Agent Turn，
        因而运行中的 steer 会在下一次 LLM snapshot 前进入 Core Memory。
        ``repository.claim`` 负责和后台 worker 做最后的原子去重。
        """
        snapshot = await self.repository.snapshot(session_id)
        candidates = tuple(snapshot.next_step)
        if not candidates or self._closed:
            return ()
        decision, discard_items = await self._before_claim_batch(
            session_id, snapshot, candidates,
        )
        if decision == "keep":
            return ()
        if decision == "discard":
            for candidate in discard_items:
                removed = await self.repository.remove(session_id, candidate.request_id)
                if removed is None:
                    continue
                await self._emit_mutation(
                    INBOX_DISCARDED_SPEC,
                    InboxMutationPayload(
                        session_id=session_id,
                        item=removed,
                        target="next-step",
                        operation="discarded",
                    ),
                )
            await self._publish(session_id)
            return ()
        return await self._claim_candidates(session_id, snapshot, candidates)

    def _wake_event(self, session_id: str) -> asyncio.Event:
        return self._wake.setdefault(session_id, asyncio.Event())

    async def _admit(
        self,
        message: InboundMessage,
        target: QueueTarget,
        *,
        wake: bool = True,
    ) -> IngressResult:
        if self._closed:
            return IngressResult(False, message.session_id, message.request_id, False, error={
                "code": "inbox-closed", "message": "Inbox 已关闭"
            })
        try:
            created, _position = await self.repository.admit(
                QueueItem(
                    request_id=message.request_id,
                    sequence=0,
                    session_id=message.session_id,
                    channel_id=message.channel_id,
                    content=message.content,
                    attachments=tuple(dict(item) for item in message.attachments),
                    source=message.source if message.source in {"user", "plugin", "system"} else "user",
                ),
                target,
            )
        except OverflowError as exc:
            return IngressResult(False, message.session_id, message.request_id, False, error={
                "code": "queue-full", "message": str(exc), "retryable": True
            })
        except ValueError as exc:
            return IngressResult(False, message.session_id, message.request_id, False, error={
                "code": "session-not-found", "message": str(exc), "retryable": False
            })
        await self._publish(message.session_id)
        if wake and message.session_id in self._blocked:
            self._blocked.pop(message.session_id, None)
            await self._publish_status_event(message.session_id, "idle")
        if created:
            admitted_snapshot = await self.repository.snapshot(message.session_id)
            admitted_item = next(
                item
                for item in admitted_snapshot.pending
                if item.request_id == message.request_id
            )
            await self._emit_mutation(
                INBOX_INSERTED_SPEC,
                InboxMutationPayload(
                    session_id=message.session_id,
                    item=admitted_item,
                    target=target,
                    operation="inserted",
                ),
            )
        # 只有 next-turn 会产生独立的 Agent Turn，因此才建立可等待 receipt。
        # next-step 的 steer/inject 可能被 Core Hook 直接注入当前 Turn；为它们
        # 创建 Future 会永远没有完成者，形成隐蔽的长期内存泄漏。
        if target == "next-turn" and created:
            key = (message.session_id, message.request_id)
            self._receipts.setdefault(key, asyncio.get_running_loop().create_future())
        if wake and (await self.repository.snapshot(message.session_id)).has_pending:
            self._ensure_worker(message.session_id)
            self._wake_event(message.session_id).set()
        return IngressResult(True, message.session_id, message.request_id, created)

    def _ensure_worker(self, session_id: str) -> None:
        if self._closed or self._agent is None:
            return
        task = self._workers.get(session_id)
        if task is None or task.done():
            self._workers[session_id] = asyncio.create_task(
                self._worker(session_id), name=f"inbox:{session_id}"
            )

    async def _worker(self, session_id: str) -> None:
        try:
            while not self._closed:
                event = self._wake_event(session_id)
                event.clear()
                snapshot = await self.repository.snapshot(session_id)
                candidates = self._candidate_batch(snapshot)
                if not candidates:
                    await event.wait()
                    continue
                if self._agent is None:
                    return
                busy = self._agent.is_busy(session_id)
                if busy:
                    await event.wait()
                    continue
                try:
                    decision, discard_items = await self._before_claim_batch(
                        session_id, snapshot, candidates
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - Hook policy keeps pending
                    self._blocked[session_id] = str(exc)
                    await self._publish_status_event(session_id, "blocked")
                    await event.wait()
                    continue
                if decision == "keep":
                    self._blocked[session_id] = "before-claim hook rejected candidate"
                    await self._publish_status_event(session_id, "blocked")
                    await event.wait()
                    continue
                if decision == "discard":
                    # discard is explicit and still happens before claim; remove only
                    # the candidates that the Hook approved for discard, then retry.
                    for candidate in discard_items:
                        await self.repository.remove(session_id, candidate.request_id)
                        await self._emit_mutation(
                            INBOX_DISCARDED_SPEC,
                            InboxMutationPayload(
                                session_id=session_id,
                                item=candidate,
                                target=(
                                    "next-step"
                                    if candidate in snapshot.next_step
                                    else "next-turn"
                                ),
                                operation="discarded",
                            ),
                        )
                    await self._publish(session_id)
                    self._blocked.pop(session_id, None)
                    continue
                if session_id in self._blocked:
                    self._blocked.pop(session_id, None)
                    await self._publish_status_event(session_id, "idle")
                try:
                    claimed = await self._claim_candidates(session_id, snapshot, candidates)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep pending on durable claim failure
                    # Claim/commit 失败时不能让 worker Task 以未处理异常结束：
                    # pending 仍属于 Inbox，记录 blocked 并等待下一次 admission
                    # 或显式 wake，保证既不丢消息也不忙循环重试。
                    self._blocked[session_id] = str(exc)
                    await self._publish_status_event(session_id, "blocked")
                    await event.wait()
                    continue
                if not claimed:
                    continue
                try:
                    # AgentService 仍逐条执行；批量 claim 只保证安全边界上的
                    # atomic ownership，避免 Hook 只放行半批后造成隐式重排。
                    for item in claimed:
                        inbound = InboundMessage(
                            session_id=item.session_id,
                            request_id=item.request_id,
                            channel_id=item.channel_id,
                            content=item.content,
                            attachments=item.attachments,
                            source=item.source,
                        )
                        try:
                            result = self._agent.run(inbound)
                            if inspect.isawaitable(result):
                                result = await result
                        except Exception as exc:
                            future = self._receipts.pop((session_id, item.request_id), None)
                            if future is not None and not future.done():
                                future.set_exception(exc)
                            logger.exception(
                                "[ftre-inbox] AgentService.run failed session=%s request=%s",
                                session_id,
                                item.request_id,
                            )
                            continue
                        future = self._receipts.pop((session_id, item.request_id), None)
                        if future is not None and not future.done():
                            future.set_result(result)
                finally:
                    await self._publish(session_id)
                    self._wake_event(session_id).set()
        except asyncio.CancelledError:
            return
        finally:
            current = self._workers.get(session_id)
            if current is asyncio.current_task():
                self._workers.pop(session_id, None)

    @staticmethod
    def _candidate_batch(snapshot: InboxSnapshot) -> tuple[QueueItem, ...]:
        """计算一次安全边界的候选：全部 next-step，加最多一条 next-turn。"""
        if snapshot.next_step:
            candidates = list(snapshot.next_step)
            if snapshot.next_turn:
                candidates.append(snapshot.next_turn[0])
            return tuple(candidates)
        return (snapshot.next_turn[0],) if snapshot.next_turn else ()

    async def _claim_candidates(
        self,
        session_id: str,
        snapshot: InboxSnapshot,
        candidates: tuple[QueueItem, ...],
    ) -> tuple[QueueItem, ...]:
        """完成 Inbox 唯一的 claim/观察/发布动作，并返回实际拥有者。"""
        claimed = await self.repository.claim(
            session_id, tuple(item.request_id for item in candidates)
        )
        if not claimed:
            return ()
        for item in claimed:
            await self._emit_mutation(
                INBOX_CLAIMED_SPEC,
                InboxMutationPayload(
                    session_id=session_id,
                    item=item,
                    target=(
                        "next-step"
                        if item in snapshot.next_step
                        else "next-turn"
                    ),
                    operation="claimed",
                ),
            )
        await self._publish(session_id)
        return claimed

    async def _before_claim_batch(
        self,
        session_id: str,
        snapshot: InboxSnapshot,
        candidates: tuple[QueueItem, ...],
    ) -> tuple[str, tuple[QueueItem, ...]]:
        """对整批候选做决策，返回 (enter/keep/discard, explicit discards)。"""
        for candidate in candidates:
            if self._before_claim is not None:
                decision = self._before_claim(candidate, snapshot)
                if inspect.isawaitable(decision):
                    decision = await decision
                if decision is False:
                    return "keep", ()
            if self._hook_runtime is None or INBOX_BEFORE_CLAIM_SPEC is None:
                continue
            decision = await self._hook_runtime.dispatch(
                INBOX_BEFORE_CLAIM_SPEC,
                BeforeClaimPayload(
                    session_id=session_id,
                    candidate=candidate,
                    target=(
                        "next-step"
                        if candidate in snapshot.next_step
                        else "next-turn"
                    ),
                    channel_id=candidate.channel_id,
                    cancellation=asyncio.Event(),
                    candidates=candidates,
                ),
            )
            if isinstance(decision, RejectClaim) and decision.disposition == "keep":
                return "keep", ()
            if not isinstance(decision, EnterClaim) or decision.request_id != candidate.request_id:
                if isinstance(decision, RejectClaim) and decision.disposition == "discard":
                    return "discard", (candidate,)
                return "keep", ()
        return "enter", ()

    async def _publish(self, session_id: str) -> None:
        if self._hook_runtime is not None and INBOX_CHANGED_SPEC is not None:
            await self._emit_mutation(
                INBOX_CHANGED_SPEC,
                InboxChangedPayload(session_id=session_id),
            )

    async def _emit_mutation(self, spec, payload: InboxMutationPayload) -> None:
        """发布观察 Hook；观察失败不改变已提交的 Inbox 事实。"""
        if self._hook_runtime is None or spec is None:
            return
        try:
            await self._hook_runtime.dispatch(spec, payload)
        except Exception:
            logger.exception(
                "[ftre-inbox] mutation hook failed session=%s operation=%s",
                payload.session_id,
                payload.operation,
            )

    def status(self, session_id: str) -> str | None:
        """返回 Inbox 自己拥有的状态；``None`` 表示交给 AgentService。"""
        return "blocked" if session_id in self._blocked else None

    async def _publish_status_event(self, session_id: str, status: str) -> None:
        if self._hook_runtime is not None and INBOX_STATUS_CHANGED_SPEC is not None:
            await self._emit_mutation(
                INBOX_STATUS_CHANGED_SPEC,
                InboxStatusPayload(session_id=session_id, status=status),
            )
