"""Inbox Service 和按 Session 串行的 worker。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ftre.services.messaging.bus import BusMessage

from ftre_agent import AgentConfig, AgentCreateSpec, AgentRunRequest

from ftre.services.messaging.bus import IngressResult

from .hooks import (
    INBOX_ADMITTED_SPEC,
    INBOX_BEFORE_ADMIT_SPEC,
    INBOX_BEFORE_CLAIM_SPEC,
    INBOX_CHANGED_SPEC,
    INBOX_CLAIMED_SPEC,
    INBOX_DEFERRED_SPEC,
    INBOX_DELIVERED_SPEC,
    INBOX_DISCARDED_SPEC,
    INBOX_ERROR_SPEC,
    INBOX_FAILED_SPEC,
    INBOX_STATUS_CHANGED_SPEC,
    AllowAdmission,
    BeforeAdmissionPayload,
    BeforeClaimPayload,
    EnterClaim,
    InboxAdmissionPayload,
    InboxChangedPayload,
    InboxClaimedPayload,
    InboxDeferredPayload,
    InboxDeliveredPayload,
    InboxDiscardedPayload,
    InboxErrorPayload,
    InboxFailedPayload,
    InboxStatusPayload,
    RejectAdmission,
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

    ``agent`` 可以实现 F35 的 ``try_reserve``/``run(agent_id, AgentRunRequest)`` 数据面；
    Inbox ingress DTO 只在本 Service 内部转换为 AgentRunRequest，QueueItem 永远不会传进
    AgentService。
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
        session_events=None,
    ) -> None:
        self.repository = repository
        self._agent = agent
        self._workers: dict[str, asyncio.Task] = {}
        self._wake: dict[str, asyncio.Event] = {}
        self._receipts: dict[tuple[str, str], asyncio.Future] = {}
        self._closed = False
        self._before_claim = before_claim
        self._hook_runtime = hook_runtime
        self._session_events = session_events
        self._blocked: dict[str, str] = {}
        self._agent_status_disposer = None
        subscribe = getattr(agent, "on_status_changed", None)
        if callable(subscribe):
            self._agent_status_disposer = subscribe(self._on_agent_status)

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
        self._session_events = None
        self._agent = None
        if self._agent_status_disposer is not None:
            self._agent_status_disposer()
            self._agent_status_disposer = None

    async def followup(self, message: InboundMessage | AgentRunRequest) -> IngressResult:
        return await self._admit(message, "next-turn")

    async def steer(self, message: InboundMessage | AgentRunRequest) -> IngressResult:
        return await self._admit(message, "next-step")

    async def inject(self, message: InboundMessage | AgentRunRequest) -> IngressResult:
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
        if mode not in {"queue", "steer"}:
            # WebSocket 会在更外层拒绝非法 mode；这里仍需守住 Bus/内部调用边界，
            # 避免一个拼写错误被静默降级成普通 queue。
            return IngressResult(
                accepted=False,
                session_id=session_id,
                request_id=str(message.metadata.request_id or message.id),
                error={
                    "code": "invalid_mode",
                    "message": "mode 只能是 queue 或 steer",
                    "retryable": False,
                },
            )
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
        return {
            "session_id": session_id,
            # revision 是客户端判断快照新旧的唯一依据；不能让客户端用本地
            # 收到顺序猜测，否则 operation response 和后台 push 乱序时会回退。
            "revision": snapshot.revision,
            "items": items,
        }

    async def edit(self, session_id: str, request_id: str, content: str, attachments=None) -> bool:
        result = await self.repository.edit(session_id, request_id, content, attachments)
        if result is None:
            return False
        await self._publish(session_id)
        self._wake_event(session_id).set()
        return True

    async def remove(self, session_id: str, request_id: str) -> bool:
        result = await self.repository.remove(session_id, request_id)
        if result is None:
            return False
        await self._publish(session_id)
        self._wake_event(session_id).set()
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
        它们没有独立的 Turn 结果；因此不创建永不完成的 receipt，调用方应使用
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
        event = self._wake_event(session_id)
        while True:
            snapshot = await self.repository.snapshot(session_id)
            busy = self._agent is not None and self._agent.is_busy(session_id)
            if not snapshot.has_pending and not busy:
                return {"session_id": session_id, "status": "quiescent"}
            event.clear()
            snapshot = await self.repository.snapshot(session_id)
            busy = self._agent is not None and self._agent.is_busy(session_id)
            if not snapshot.has_pending and not busy:
                return {"session_id": session_id, "status": "quiescent"}
            await event.wait()

    async def claim_next_step_for_reasoning(
        self,
        session_id: str,
    ) -> tuple[QueueItem, ...]:
        """交付当前 active Turn 的 ``next-step`` 消息。

        该入口只由 ``agent/before-reasoning`` Hook 调用。它复用同一套
        ``peek → before-claim → history upsert → claim`` 逻辑，但不会启动第二个
        Agent Turn，因而运行中的 steer 会在下一次 LLM snapshot 前进入 Core Memory。
        ``session_events`` 存在时，正式 UserMessage 先幂等落库并广播，再从 Inbox
        claim；这样 claim 后崩溃也不会同时丢失 pending 和聊天历史。
        """
        return await self.deliver_next_step_for_reasoning(session_id)

    async def deliver_next_step_for_reasoning(
        self,
        session_id: str,
        *,
        turn_id: str = "",
    ) -> tuple[QueueItem, ...]:
        """在下一次 Reasoning 前完成 next-step 的 DB-first 交付。

        ``session_events`` 是 Host 提供的稳定历史/广播出口。独立 Package 测试在
        未提供它时仍保留原子 claim 能力；Gateway Composition 始终注入该 Service，
        生产路径因此不会回到“先 claim 后落库”的旧顺序。
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
                await self._dispatch_observe(
                    INBOX_DISCARDED_SPEC,
                    InboxDiscardedPayload(
                        session_id=session_id,
                        request_id=candidate.request_id,
                        reason="before-claim-discard",
                    ),
                )
            await self._publish(session_id)
            return ()
        history_ids = await self._persist_user_messages(candidates, run_id=turn_id)
        claimed = await self._claim_candidates(session_id, snapshot, candidates)
        return self._attach_history_ids(claimed, history_ids)

    async def _persist_user_messages(
        self,
        candidates: tuple[QueueItem, ...],
        *,
        run_id: str = "",
    ) -> dict[str, str]:
        """在安全 Reasoning 边界持久化正式 UserMessage，并返回稳定 id。"""
        if self._session_events is None:
            return {}
        history_ids: dict[str, str] = {}
        previous_assistant_id = None
        if hasattr(self._session_events, "active_assistant_message_id"):
            previous_assistant_id = await self._session_events.active_assistant_message_id(
                candidates[0].session_id
            )
        for candidate in candidates:
            # Plugin inject 只贡献上下文，不伪造用户历史；真正的 role=user 才会
            # 触发 Core 的 Assistant message_id 边界。
            if candidate.source != "user":
                continue
            result = await self._session_events.emit_user_message_if_absent(
                candidate.session_id,
                candidate.channel_id,
                request_id=candidate.request_id,
                content=candidate.content,
                attachments=candidate.attachments,
                source=candidate.source,
                run_id=run_id,
                previous_assistant_message_id=previous_assistant_id,
            )
            persisted = getattr(result, "persisted_messages", ()) or ()
            if persisted:
                history_ids[candidate.request_id] = persisted[0].id
            previous_assistant_id = None
        return history_ids

    @staticmethod
    def _attach_history_ids(
        claimed: tuple[QueueItem, ...],
        history_ids: dict[str, str],
    ) -> tuple[QueueItem, ...]:
        if not history_ids:
            return claimed
        return tuple(
            replace(
                item,
                history_message_id=history_ids.get(item.request_id),
            )
            for item in claimed
        )

    def _wake_event(self, session_id: str) -> asyncio.Event:
        return self._wake.setdefault(session_id, asyncio.Event())

    async def _admit(
        self,
        message: InboundMessage | AgentRunRequest,
        target: QueueTarget,
        *,
        wake: bool = True,
    ) -> IngressResult:
        if self._closed:
            return IngressResult(False, message.session_id, message.request_id, False, error={
                "code": "inbox-closed", "message": "Inbox 已关闭"
            })
        metadata = dict(message.metadata or {})
        agent_id = str(metadata.get("agent_id") or "default")
        messages = tuple(getattr(message, "messages", ()) or ())
        content = getattr(message, "content", "")
        attachments = getattr(message, "attachments", ())
        if isinstance(message, AgentRunRequest):
            content = "\n".join(
                text for text in (item.get_text_content() or "" for item in messages) if text
            )
            attachments = metadata.get("attachments", ())
        candidate_item = QueueItem(
            request_id=message.request_id,
            sequence=0,
            session_id=message.session_id,
            channel_id=message.channel_id,
            content=content,
            attachments=tuple(dict(item) for item in attachments),
            source=message.source if message.source in {"user", "plugin", "system"} else "user",
            messages=tuple(messages),
            agent_id=agent_id,
        )
        if self._hook_runtime is not None and INBOX_BEFORE_ADMIT_SPEC is not None:
            decision = await self._hook_runtime.dispatch(
                INBOX_BEFORE_ADMIT_SPEC,
                BeforeAdmissionPayload(
                    session_id=message.session_id,
                    request_id=message.request_id,
                    target=target,
                    item=candidate_item,
                ),
            )
            if isinstance(decision, RejectAdmission):
                return IngressResult(
                    False,
                    message.session_id,
                    message.request_id,
                    False,
                    error={
                        "code": "admission-rejected",
                        "message": decision.reason,
                        "retryable": False,
                    },
                )
            if not isinstance(decision, AllowAdmission):
                raise TypeError("inbox/before-admit must return AllowAdmission or RejectAdmission")
        try:
            created, _position = await self.repository.admit(
                candidate_item,
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
        snapshot = await self.repository.snapshot(message.session_id)
        admitted_item = next(
            (item for item in snapshot.pending if item.request_id == message.request_id),
            None,
        )
        if admitted_item is not None:
            await self._dispatch_observe(
                INBOX_ADMITTED_SPEC,
                InboxAdmissionPayload(
                    session_id=message.session_id,
                    request_id=message.request_id,
                    target=target,
                    item=admitted_item,
                    created=created,
                ),
            )
        if wake and message.session_id in self._blocked:
            self._blocked.pop(message.session_id, None)
            await self._publish_status_event(message.session_id, "idle")
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

    def _on_agent_status(self, view: Any) -> None:
        session_id = getattr(view, "session_id", None) or getattr(view, "agent_id", None)
        if session_id:
            self._wake_event(str(session_id)).set()

    @staticmethod
    def _supports_reservation(agent: Any) -> bool:
        return callable(getattr(agent, "try_reserve", None)) and callable(
            getattr(agent, "release_reservation", None)
        )

    @staticmethod
    def _execution_agent_id(item: QueueItem) -> str:
        return f"{item.session_id}:{item.agent_id or 'default'}"

    @staticmethod
    def _run_result_info(result: Any) -> tuple[str, str, bool]:
        if isinstance(result, dict):
            status = str(result.get("status") or "completed")
            error = result.get("error")
        else:
            status = str(getattr(result, "status", "completed"))
            error = getattr(result, "error", None)
        if isinstance(error, dict):
            reason = str(error.get("message") or error.get("code") or status)
            retryable = bool(error.get("retryable", status != "failed"))
        else:
            reason = str(error or status)
            retryable = status != "failed"
        return status, reason, retryable

    async def _ensure_agent(self, item: QueueItem) -> str:
        """为一个 Session 建立 AgentService identity；Runtime profile 仍独立解析。"""
        agent = self._agent
        if agent is None:
            raise RuntimeError("Inbox AgentService unavailable")
        agent_id = self._execution_agent_id(item)
        get = getattr(agent, "get", None)
        if callable(get) and get(agent_id) is None:
            create = getattr(agent, "create", None)
            if not callable(create):
                raise RuntimeError("AgentService cannot create an execution identity")
            await create(
                AgentCreateSpec(
                    agent_id=agent_id,
                    config=AgentConfig(),
                    session_id=item.session_id,
                    metadata={"profile_agent_id": item.agent_id or "default"},
                )
            )
        return agent_id

    def _to_agent_request(self, item: QueueItem, agent_id: str) -> AgentRunRequest:
        metadata: dict[str, Any] = {
            "agent_id": item.agent_id or "default",
            "profile_agent_id": item.agent_id or "default",
            "attachments": tuple(dict(value) for value in item.attachments),
        }
        if item.history_message_id:
            metadata["history_message_id"] = item.history_message_id
        return AgentRunRequest(
            session_id=item.session_id,
            request_id=item.request_id,
            messages=item.normalized_messages(),
            agent_id=agent_id,
            channel_id=item.channel_id,
            source=item.source,
            metadata=metadata,
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
                if session_id in self._blocked:
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
                    await self._dispatch_observe(
                        INBOX_DEFERRED_SPEC,
                        InboxDeferredPayload(
                            session_id=session_id,
                            request_id=candidates[0].request_id,
                            reason="before-claim-rejected",
                        ),
                    )
                    await self._publish_status_event(session_id, "blocked")
                    await event.wait()
                    continue
                if decision == "discard":
                    # discard is explicit and still happens before claim; remove only
                    # the candidates that the Hook approved for discard, then retry.
                    for candidate in discard_items:
                        await self.repository.remove(session_id, candidate.request_id)
                        await self._dispatch_observe(
                            INBOX_DISCARDED_SPEC,
                            InboxDiscardedPayload(
                                session_id=session_id,
                                request_id=candidate.request_id,
                                reason="before-claim-discard",
                            ),
                        )
                    await self._publish(session_id)
                    self._blocked.pop(session_id, None)
                    continue
                if session_id in self._blocked:
                    self._blocked.pop(session_id, None)
                    await self._publish_status_event(session_id, "idle")
                structured = self._supports_reservation(self._agent)
                reservation = None
                lease_id = None
                execution_agent_id = None
                try:
                    if structured:
                        candidate = candidates[0]
                        execution_agent_id = await self._ensure_agent(candidate)
                        reservation = self._agent.try_reserve(
                            execution_agent_id,
                            session_id,
                            candidate.request_id,
                        )
                        if reservation is None:
                            await self._dispatch_observe(
                                INBOX_DEFERRED_SPEC,
                                InboxDeferredPayload(
                                    session_id=session_id,
                                    request_id=candidates[0].request_id,
                                    reason="agent-busy",
                                ),
                            )
                            await event.wait()
                            continue
                        history_ids = await self._persist_user_messages((candidate,))
                        leases = await self.repository.claim_lease(
                            session_id,
                            (candidate.request_id,),
                        )
                        if not leases:
                            self._agent.release_reservation(reservation)
                            continue
                        lease_id = leases[0].lease_id
                        claimed = self._attach_history_ids(
                            tuple(lease.item for lease in leases), history_ids
                        )
                        await self._publish(session_id)
                        await self._dispatch_observe(
                            INBOX_CLAIMED_SPEC,
                            InboxClaimedPayload(
                                session_id=session_id,
                                request_ids=tuple(item.request_id for item in claimed),
                                lease_id=lease_id,
                            ),
                        )
                    else:
                        history_ids = await self._persist_user_messages(candidates)
                        claimed = await self._claim_candidates(session_id, snapshot, candidates)
                        claimed = self._attach_history_ids(claimed, history_ids)
                except asyncio.CancelledError:
                    if reservation is not None:
                        self._agent.release_reservation(reservation)
                    raise
                except Exception as exc:  # noqa: BLE001 - keep pending on durable claim failure
                    if reservation is not None:
                        self._agent.release_reservation(reservation)
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
                        try:
                            if structured:
                                result = self._agent.run(
                                    execution_agent_id,
                                    self._to_agent_request(item, execution_agent_id),
                                )
                            else:
                                result = self._agent.run(
                                    self._to_agent_request(
                                        item,
                                        execution_agent_id or self._execution_agent_id(item),
                                    )
                                )
                            if inspect.isawaitable(result):
                                result = await result
                            status, reason, retryable = self._run_result_info(result)
                            if status in {"failed", "cancelled", "interrupted"}:
                                if reservation is not None:
                                    self._agent.release_reservation(reservation)
                                    reservation = None
                                if lease_id is not None:
                                    if retryable or status != "failed":
                                        await self.repository.release(session_id, lease_id)
                                    else:
                                        await self.repository.ack(session_id, lease_id)
                                    lease_id = None
                                await self._dispatch_observe(
                                    INBOX_ERROR_SPEC,
                                    InboxErrorPayload(
                                        session_id=session_id,
                                        request_id=item.request_id,
                                        stage="agent-run",
                                        error=reason,
                                        retryable=retryable,
                                    ),
                                )
                                await self._dispatch_observe(
                                    INBOX_FAILED_SPEC,
                                    InboxFailedPayload(
                                        session_id=session_id,
                                        request_id=item.request_id,
                                        reason=reason,
                                    ),
                                )
                                if retryable or status != "failed":
                                    self._blocked[session_id] = reason
                                    await self._publish_status_event(session_id, "blocked")
                                future = self._receipts.pop((session_id, item.request_id), None)
                                if future is not None and not future.done():
                                    future.set_result(result)
                                continue
                        except Exception as exc:
                            if reservation is not None:
                                self._agent.release_reservation(reservation)
                            if lease_id is not None:
                                await self.repository.release(session_id, lease_id)
                                lease_id = None
                            await self._dispatch_observe(
                                INBOX_ERROR_SPEC,
                                InboxErrorPayload(
                                    session_id=session_id,
                                    request_id=item.request_id,
                                    stage="agent-run",
                                    error=str(exc),
                                ),
                            )
                            await self._dispatch_observe(
                                INBOX_FAILED_SPEC,
                                InboxFailedPayload(
                                    session_id=session_id,
                                    request_id=item.request_id,
                                    reason=str(exc),
                                ),
                            )
                            future = self._receipts.pop((session_id, item.request_id), None)
                            if future is not None and not future.done():
                                future.set_exception(exc)
                            logger.exception(
                                "[ftre-inbox] AgentService.run failed session=%s request=%s",
                                session_id,
                                item.request_id,
                            )
                            continue
                        if lease_id is not None:
                            await self.repository.ack(session_id, lease_id)
                            await self._dispatch_observe(
                                INBOX_DELIVERED_SPEC,
                                InboxDeliveredPayload(
                                    session_id=session_id,
                                    request_id=item.request_id,
                                    lease_id=lease_id,
                                    status=(
                                        str(getattr(result, "status", None) or result.get("status", "completed"))
                                        if isinstance(result, dict)
                                        else str(getattr(result, "status", "completed"))
                                    ),
                                ),
                            )
                            lease_id = None
                        future = self._receipts.pop((session_id, item.request_id), None)
                        if future is not None and not future.done():
                            future.set_result(result)
                finally:
                    if lease_id is not None:
                        await self.repository.release(session_id, lease_id)
                        lease_id = None
                    await self._publish(session_id)
                    if session_id not in self._blocked:
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
            # changed 是已持久化事实；PARALLEL dispatch 返回前，协议适配器必须
            # 完成当前 revision 的权威快照读取，不能再用 detached EMIT 乱序发送。
            await self._hook_runtime.dispatch(
                INBOX_CHANGED_SPEC,
                InboxChangedPayload(session_id=session_id),
            )

    async def _dispatch_observe(self, spec, payload) -> None:
        if self._hook_runtime is None or spec is None:
            return
        try:
            await self._hook_runtime.dispatch(spec, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[ftre-inbox] observe hook failed: %s", spec.name)

    def status(self, session_id: str) -> str | None:
        """返回 Inbox 自己拥有的状态；``None`` 表示交给 AgentService。"""
        return "blocked" if session_id in self._blocked else None

    async def _publish_status_event(self, session_id: str, status: str) -> None:
        if self._hook_runtime is not None and INBOX_STATUS_CHANGED_SPEC is not None:
            await self._hook_runtime.dispatch(
                INBOX_STATUS_CHANGED_SPEC,
                InboxStatusPayload(session_id=session_id, status=status),
            )
