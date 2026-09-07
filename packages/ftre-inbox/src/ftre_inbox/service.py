"""将外部输入持久化排队，并在明确的 Agent 边界交付。"""

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
    RejectAdmission,
    RejectClaim,
)
from .models import InboxSnapshot, QueueItem, QueueTarget
from .protocol import InboundMessage
from .repository import InboxRepository

logger = logging.getLogger(__name__)


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(
            str(part.get("text", ""))
            for part in value
            if isinstance(part, dict) and part.get("type", "text") == "text"
        )
    return str(value or "")


class InboxService:
    """Inbox 只拥有 admission、持久化、claim 和快照。

    下一步输入由 ``agent/before-reasoning`` 领取；下一轮输入由
    ``agent/after-run(status=completed)`` 触发一次交付。这里没有自主 worker、
    Agent 状态订阅或 Inbox 阻塞状态。
    """

    key = "inbox"
    changed_hook_spec = INBOX_CHANGED_SPEC

    def __init__(
        self,
        repository: InboxRepository,
        agent=None,
        *,
        hook_runtime=None,
        before_claim=None,
        sessions=None,
    ) -> None:
        self.repository = repository
        self._agent = agent
        self._hook_runtime = hook_runtime
        self._before_claim = before_claim
        self._sessions = sessions
        self._closed = False
        self._dispatch_tasks: dict[str, asyncio.Task] = {}
        self._dispatch_requested: set[str] = set()
        self._dispatch_wait_for_idle: set[str] = set()
        self._receipts: dict[tuple[str, str], asyncio.Future] = {}

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        """加载 pending；恢复只恢复数据，不自动触发 Agent。"""
        await self.repository.load_all()

    async def close(self) -> None:
        self._closed = True
        tasks = tuple(self._dispatch_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._dispatch_tasks.clear()
        self._dispatch_requested.clear()
        self._dispatch_wait_for_idle.clear()
        for future in self._receipts.values():
            if not future.done():
                future.cancel()
        self._receipts.clear()
        self.repository.close()
        self._before_claim = None
        self._hook_runtime = None
        self._sessions = None
        self._agent = None

    async def followup(self, message: InboundMessage | AgentRunRequest) -> IngressResult:
        was_busy = self._agent_busy(message.session_id)
        result = await self._admit(message, "next-turn")
        if result.created and not was_busy and self._agent_can_receive(result.session_id):
            self.schedule_next_turn(result.session_id)
        return result

    async def steer(self, message: InboundMessage | AgentRunRequest) -> IngressResult:
        return await self._admit(message, "next-step")

    async def inject(self, message: InboundMessage | AgentRunRequest) -> IngressResult:
        return await self._admit(message, "next-step")

    async def snapshot(self, session_id: str) -> InboxSnapshot:
        return await self.repository.snapshot(session_id)

    async def delete_session(self, session_id: str) -> None:
        task = self._dispatch_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._dispatch_requested.discard(session_id)
        self._dispatch_wait_for_idle.discard(session_id)
        for key, future in tuple(self._receipts.items()):
            if key[0] == session_id:
                if not future.done():
                    future.cancel()
                self._receipts.pop(key, None)
        await self.repository.delete_session(session_id)

    async def handle_bus_message(self, message: BusMessage) -> IngressResult:
        data = (
            message.data.model_dump(mode="json")
            if hasattr(message.data, "model_dump")
            else dict(message.data or {})
        )
        session_id = str(data.get("session_id") or message.from_session)
        request_id = str(message.metadata.request_id or message.id)
        if message.type == "turn_cancel":
            cancelled = await self.cancel(
                session_id,
                str(data.get("request_id") or "") or None,
            )
            return IngressResult(
                accepted=True,
                session_id=session_id,
                request_id=str(message.metadata.request_id or ""),
                created=cancelled,
            )
        inbound = InboundMessage(
            session_id=session_id,
            request_id=request_id,
            channel_id=str(message.from_channel),
            content=_content_text(data.get("content")),
            attachments=tuple(dict(item) for item in (data.get("attachments") or ())),
            source=str(data.get("source") or "user"),
            metadata=message.metadata.model_dump(mode="json"),
        )
        mode = str(data.get("mode") or "queue")
        if mode == "queue":
            return await self.followup(inbound)
        if mode == "steer":
            return await self.steer(inbound)
        return IngressResult(
            accepted=False,
            session_id=session_id,
            request_id=request_id,
            error={
                "code": "invalid_mode",
                "message": "mode 只能是 queue 或 steer",
                "retryable": False,
            },
        )

    async def wire_snapshot(self, session_id: str) -> dict[str, Any]:
        snapshot = await self.repository.snapshot(session_id)
        next_turn_ids = {item.request_id for item in snapshot.next_turn}
        return {
            "session_id": session_id,
            "revision": snapshot.revision,
            "items": [
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
            ],
        }

    async def edit(self, session_id: str, request_id: str, content: str, attachments=None) -> bool:
        item = await self.repository.edit(session_id, request_id, content, attachments)
        if item is None:
            return False
        await self._publish(session_id)
        return True

    async def remove(self, session_id: str, request_id: str) -> bool:
        item = await self.repository.remove(session_id, request_id)
        if item is None:
            return False
        await self._publish(session_id)
        return True

    async def promote(self, session_id: str, request_id: str) -> bool:
        item = await self.repository.promote(session_id, request_id)
        if item is None:
            return False
        await self._publish(session_id)
        return True

    async def cancel(self, session_id: str, request_id: str | None = None) -> bool:
        if request_id:
            return await self.remove(session_id, request_id)
        if self._agent is None:
            return False
        result = self._agent.cancel(session_id)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    async def wait(self, session_id: str, request_id: str):
        future = self._receipts.get((session_id, request_id))
        if future is None:
            raise ValueError("只有 followup/next-turn 输入提供可等待的 Turn receipt")
        return await future

    async def wait_session_quiescent(self, session_id: str):
        """等待队列为空且 Agent 没有 active Turn。"""
        while not self._closed:
            snapshot = await self.repository.snapshot(session_id)
            if not snapshot.has_pending and not self._agent_busy(session_id):
                return {"session_id": session_id, "status": "quiescent"}
            await asyncio.sleep(0.05)
        return {"session_id": session_id, "status": "closed"}

    async def claim_next_step_for_reasoning(self, session_id: str) -> tuple[QueueItem, ...]:
        return await self.deliver_next_step_for_reasoning(session_id)

    async def deliver_next_step_for_reasoning(self, session_id: str) -> tuple[QueueItem, ...]:
        if self._closed:
            return ()
        snapshot = await self.repository.snapshot(session_id)
        candidates = snapshot.next_step
        if not candidates:
            return ()
        decision, discarded = await self._before_claim_batch(session_id, snapshot, candidates)
        if decision == "keep":
            await self._defer(session_id, candidates[0], "before-claim-rejected")
            return ()
        if decision == "discard":
            await self._discard(session_id, discarded, "before-claim-discard")
            return ()
        history_ids = await self._persist_user_messages(candidates)
        claimed = await self.repository.claim(
            session_id,
            tuple(item.request_id for item in candidates),
        )
        if not claimed:
            return ()
        claimed = self._attach_history_ids(claimed, history_ids)
        await self._publish(session_id)
        await self._observe(
            INBOX_CLAIMED_SPEC,
            InboxClaimedPayload(
                session_id=session_id,
                request_ids=tuple(item.request_id for item in claimed),
            ),
        )
        return claimed

    @staticmethod
    def _candidate_batch(snapshot: InboxSnapshot) -> tuple[QueueItem, ...]:
        """返回一个新 Turn 可观察的候选形状（next-step 全量加一条 next-turn）。"""
        if snapshot.next_step:
            return (*snapshot.next_step, *snapshot.next_turn[:1])
        return snapshot.next_turn[:1]

    def schedule_next_turn(self, session_id: str, *, wait_for_idle: bool = False) -> None:
        """请求一次 next-turn 交付；同一 Session 的请求自动合并。"""
        if self._closed or self._agent is None:
            return
        self._dispatch_requested.add(session_id)
        if wait_for_idle:
            self._dispatch_wait_for_idle.add(session_id)
        task = self._dispatch_tasks.get(session_id)
        if task is None or task.done():
            self._dispatch_tasks[session_id] = asyncio.create_task(
                self._dispatch_next_turn(session_id),
                name=f"inbox-next-turn:{session_id}",
            )

    def handle_after_run(self, session_id: str, status: str, *, paused: bool = False) -> None:
        """只把自然完成映射为一次 next-turn 触发。"""
        if status == "completed" and not paused:
            self.schedule_next_turn(session_id, wait_for_idle=True)

    async def _dispatch_next_turn(self, session_id: str) -> None:
        wait_for_idle = session_id in self._dispatch_wait_for_idle
        self._dispatch_requested.discard(session_id)
        self._dispatch_wait_for_idle.discard(session_id)
        try:
            if wait_for_idle:
                while not self._closed and self._agent_busy(session_id):
                    await asyncio.sleep(0.01)
                if self._closed or self._agent_paused(session_id):
                    return
            elif not self._agent_can_receive(session_id):
                return

            snapshot = await self.repository.snapshot(session_id)
            candidate = snapshot.next_turn[:1]
            if not candidate:
                return
            decision, discarded = await self._before_claim_batch(
                session_id, snapshot, candidate,
            )
            if decision == "keep":
                await self._defer(session_id, candidate[0], "before-claim-rejected")
                return
            if decision == "discard":
                await self._discard(session_id, discarded, "before-claim-discard")
                return
            history_ids = await self._persist_user_messages(candidate)
            claimed = await self.repository.claim(
                session_id,
                tuple(item.request_id for item in candidate),
            )
            if not claimed:
                return
            claimed = self._attach_history_ids(claimed, history_ids)
            await self._publish(session_id)
            await self._observe(
                INBOX_CLAIMED_SPEC,
                InboxClaimedPayload(
                    session_id=session_id,
                    request_ids=tuple(item.request_id for item in claimed),
                ),
            )
            await self._deliver(session_id, claimed)
            await self._publish(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep pending and make failure observable
            await self._observe(
                INBOX_ERROR_SPEC,
                InboxErrorPayload(
                    session_id=session_id,
                    request_id="",
                    stage="next-turn-claim",
                    error=str(exc),
                    retryable=True,
                ),
            )
            logger.exception("[ftre-inbox] next-turn dispatch failed session=%s", session_id)
        finally:
            current = asyncio.current_task()
            if self._dispatch_tasks.get(session_id) is current:
                self._dispatch_tasks.pop(session_id, None)
            if session_id in self._dispatch_requested and not self._closed:
                wait = session_id in self._dispatch_wait_for_idle
                self._dispatch_requested.discard(session_id)
                self._dispatch_wait_for_idle.discard(session_id)
                self.schedule_next_turn(session_id, wait_for_idle=wait)

    async def deliver_one(self, session_id: str, item: QueueItem) -> bool:
        """测试和宿主使用的单项交付入口；不会重新入队。"""
        return await self._deliver(session_id, (item,))

    async def _admit(
        self,
        message: InboundMessage | AgentRunRequest,
        target: QueueTarget,
    ) -> IngressResult:
        if self._closed:
            return IngressResult(
                False,
                message.session_id,
                message.request_id,
                False,
                error={"code": "inbox-closed", "message": "Inbox 已关闭"},
            )
        item = self._item_from_message(message, target)
        if self._hook_runtime is not None and INBOX_BEFORE_ADMIT_SPEC is not None:
            decision = await self._hook_runtime.dispatch(
                INBOX_BEFORE_ADMIT_SPEC,
                BeforeAdmissionPayload(
                    session_id=item.session_id,
                    request_id=item.request_id,
                    target=target,
                    item=item,
                ),
            )
            if isinstance(decision, RejectAdmission):
                return IngressResult(
                    False,
                    item.session_id,
                    item.request_id,
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
            created, _ = await self.repository.admit(item, target)
        except OverflowError as exc:
            return IngressResult(
                False,
                item.session_id,
                item.request_id,
                False,
                error={"code": "queue-full", "message": str(exc), "retryable": True},
            )
        except ValueError as exc:
            return IngressResult(
                False,
                item.session_id,
                item.request_id,
                False,
                error={"code": "session-not-found", "message": str(exc), "retryable": False},
            )

        snapshot = await self.repository.snapshot(item.session_id)
        await self._publish(item.session_id)
        admitted = next(
            (candidate for candidate in snapshot.pending if candidate.request_id == item.request_id),
            None,
        )
        if admitted is not None:
            await self._observe(
                INBOX_ADMITTED_SPEC,
                InboxAdmissionPayload(
                    session_id=item.session_id,
                    request_id=item.request_id,
                    target=target,
                    item=admitted,
                    created=created,
                ),
            )
        if target == "next-turn" and created:
            self._receipts.setdefault(
                (item.session_id, item.request_id),
                asyncio.get_running_loop().create_future(),
            )
        return IngressResult(True, item.session_id, item.request_id, created)

    def _item_from_message(
        self,
        message: InboundMessage | AgentRunRequest,
        target: QueueTarget,
    ) -> QueueItem:
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
        return QueueItem(
            request_id=message.request_id,
            sequence=0,
            session_id=message.session_id,
            channel_id=message.channel_id,
            content=content,
            attachments=tuple(dict(item) for item in attachments),
            source=message.source if message.source in {"user", "plugin", "system"} else "user",
            messages=messages,
            agent_id=agent_id,
        )

    async def _deliver(self, session_id: str, items: tuple[QueueItem, ...]) -> bool:
        for item in items:
            try:
                agent_id = await self._ensure_agent(item)
                request = self._to_agent_request(item, agent_id)
                result = (
                    self._agent.run(agent_id, request)
                    if self._uses_agent_service()
                    else self._agent.run(request)
                )
                if inspect.isawaitable(result):
                    result = await result
                status, reason, retryable = self._run_result_info(result)
                if self._result_paused(result):
                    self._resolve(item, result)
                    return False
                if status in {"failed", "error", "cancelled", "interrupted"}:
                    await self._report_failure(session_id, item, reason, retryable)
                    self._resolve(item, result)
                    return False
                await self._observe(
                    INBOX_DELIVERED_SPEC,
                    InboxDeliveredPayload(
                        session_id=session_id,
                        request_id=item.request_id,
                        status=status,
                    ),
                )
                self._resolve(item, result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._report_failure(session_id, item, str(exc), False)
                self._resolve_exception(item, exc)
                logger.exception(
                    "[ftre-inbox] AgentService.run failed session=%s request=%s",
                    session_id,
                    item.request_id,
                )
                return False
        return True

    async def _persist_user_messages(self, candidates: tuple[QueueItem, ...]) -> dict[str, str]:
        if self._sessions is None:
            return {}
        history_ids: dict[str, str] = {}
        previous_assistant_id = None
        append_user = getattr(self._sessions, "append_user_message_if_absent", None)
        active_id = getattr(self._sessions, "log", None)
        if callable(active_id) and callable(append_user):
            try:
                log = await active_id(candidates[0].session_id)
                previous_assistant_id = next(
                    (
                        event.get("message_id")
                        for event in reversed(log.events)
                        if event.get("type") == "assistant/message"
                    ),
                    None,
                )
            except Exception:  # noqa: BLE001 - best-effort context only
                previous_assistant_id = None
        for candidate in candidates:
            if candidate.source != "user" or not callable(append_user):
                continue
            content_parts = candidate.content
            if isinstance(content_parts, str):
                content_parts = [{"type": "text", "text": content_parts}]
            result = await append_user(
                candidate.session_id,
                request_id=candidate.request_id,
                content=list(content_parts),
                metadata={
                    "hide": False,
                    "request_id": candidate.request_id,
                    "source": candidate.source,
                    "agent_id": candidate.agent_id or "default",
                    **(
                        {"previous_assistant_message_id": previous_assistant_id}
                        if previous_assistant_id
                        else {}
                    ),
                },
                previous_assistant_message_id=previous_assistant_id,
            )
            if result is not None:
                history_ids[candidate.request_id] = str(result.get("message_id") or "")
            previous_assistant_id = None
        return history_ids

    async def _before_claim_batch(
        self,
        session_id: str,
        snapshot: InboxSnapshot,
        candidates: tuple[QueueItem, ...],
    ) -> tuple[str, tuple[QueueItem, ...]]:
        next_step_ids = {item.request_id for item in snapshot.next_step}
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
                    target="next-step" if candidate.request_id in next_step_ids else "next-turn",
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

    async def _discard(
        self,
        session_id: str,
        items: tuple[QueueItem, ...],
        reason: str,
    ) -> None:
        for item in items:
            removed = await self.repository.remove(session_id, item.request_id)
            if removed is None:
                continue
            await self._observe(
                INBOX_DISCARDED_SPEC,
                InboxDiscardedPayload(
                    session_id=session_id,
                    request_id=item.request_id,
                    reason=reason,
                ),
            )
        await self._publish(session_id)

    async def _defer(self, session_id: str, item: QueueItem, reason: str) -> None:
        await self._observe(
            INBOX_DEFERRED_SPEC,
            InboxDeferredPayload(
                session_id=session_id,
                request_id=item.request_id,
                reason=reason,
            ),
        )

    async def _report_failure(
        self,
        session_id: str,
        item: QueueItem,
        reason: str,
        retryable: bool,
    ) -> None:
        await self._observe(
            INBOX_ERROR_SPEC,
            InboxErrorPayload(
                session_id=session_id,
                request_id=item.request_id,
                stage="agent-run",
                error=reason,
                retryable=retryable,
            ),
        )
        await self._observe(
            INBOX_FAILED_SPEC,
            InboxFailedPayload(
                session_id=session_id,
                request_id=item.request_id,
                reason=reason,
            ),
        )

    async def _publish(self, session_id: str) -> None:
        if self._hook_runtime is not None and INBOX_CHANGED_SPEC is not None:
            await self._hook_runtime.dispatch(
                INBOX_CHANGED_SPEC,
                InboxChangedPayload(session_id=session_id),
            )

    async def _observe(self, spec, payload) -> None:
        if self._hook_runtime is None or spec is None:
            return
        try:
            await self._hook_runtime.dispatch(spec, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[ftre-inbox] observe hook failed: %s", spec.name)

    def _agent_busy(self, session_id: str) -> bool:
        if self._agent is None:
            return False
        list_agents = getattr(self._agent, "list", None)
        if callable(list_agents):
            try:
                views = tuple(list_agents())
            except Exception:  # noqa: BLE001 - diagnostic gate only
                views = ()
            for view in views:
                if getattr(view, "session_id", None) != session_id:
                    continue
                if str(getattr(view, "state", "")) in {
                    "running",
                    "processing",
                    "compacting",
                    "paused",
                    "stopping",
                    "awaiting_confirmation",
                }:
                    return True
        busy = getattr(self._agent, "is_busy", None)
        if not callable(busy):
            return False
        try:
            return bool(busy(session_id))
        except Exception:  # noqa: BLE001 - diagnostic gate only
            return True

    def _agent_paused(self, session_id: str) -> bool:
        if self._agent is None:
            return False
        list_agents = getattr(self._agent, "list", None)
        if callable(list_agents):
            try:
                if any(
                    getattr(view, "session_id", None) == session_id
                    and str(getattr(view, "state", "")) == "paused"
                    for view in list_agents()
                ):
                    return True
            except Exception:
                logger.debug("[ftre-inbox] paused status probe failed", exc_info=True)
        status = getattr(self._agent, "status", None)
        if not callable(status):
            return False
        try:
            return str(status(session_id)) == "paused"
        except Exception:  # noqa: BLE001 - diagnostic gate only
            return False

    def _agent_can_receive(self, session_id: str) -> bool:
        return self._agent is not None and not self._agent_busy(session_id)

    def _uses_agent_service(self) -> bool:
        return self._agent is not None and callable(getattr(self._agent, "get", None))

    async def _ensure_agent(self, item: QueueItem) -> str:
        if self._agent is None:
            raise RuntimeError("Inbox AgentService unavailable")
        agent_id = f"{item.session_id}:{item.agent_id or 'default'}"
        get = getattr(self._agent, "get", None)
        if callable(get) and get(agent_id) is None:
            create = getattr(self._agent, "create", None)
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

    @staticmethod
    def _attach_history_ids(
        claimed: tuple[QueueItem, ...],
        history_ids: dict[str, str],
    ) -> tuple[QueueItem, ...]:
        return tuple(
            replace(item, history_message_id=history_ids[item.request_id])
            if item.request_id in history_ids
            else item
            for item in claimed
        )

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

    @staticmethod
    def _result_paused(result: Any) -> bool:
        return bool(result.get("paused", False)) if isinstance(result, dict) else bool(
            getattr(result, "paused", False)
        )

    def _resolve(self, item: QueueItem, result: Any) -> None:
        future = self._receipts.pop((item.session_id, item.request_id), None)
        if future is not None and not future.done():
            future.set_result(result)

    def _resolve_exception(self, item: QueueItem, error: Exception) -> None:
        future = self._receipts.pop((item.session_id, item.request_id), None)
        if future is not None and not future.done():
            future.set_exception(error)


__all__ = ["InboxService"]
