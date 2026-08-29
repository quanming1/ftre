"""将外部输入持久化排队，并按 Session 顺序交给 AgentService。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field, replace
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
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(part.get("text", ""))
            for part in value
            if isinstance(part, dict) and part.get("type", "text") == "text"
        )
    return str(value or "")


@dataclass(slots=True)
class _SessionState:
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task | None = None
    blocked_reason: str | None = None


class InboxService:
    """Inbox 的唯一职责：接纳、排队、claim，然后交给 AgentService。"""

    key = "inbox"
    changed_hook_spec = INBOX_CHANGED_SPEC
    status_hook_spec = INBOX_STATUS_CHANGED_SPEC

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
        self._hook_runtime = hook_runtime
        self._before_claim = before_claim
        self._session_events = session_events
        self._closed = False
        self._sessions: dict[str, _SessionState] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._receipts: dict[tuple[str, str], asyncio.Future] = {}
        self._agent_status_disposer = None
        subscribe = getattr(agent, "on_status_changed", None)
        if callable(subscribe):
            self._agent_status_disposer = subscribe(self._on_agent_status)

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        await self.repository.load_all()

    async def close(self) -> None:
        self._closed = True
        tasks = tuple(self._workers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._sessions.clear()
        for future in self._receipts.values():
            if not future.done():
                future.cancel()
        self._receipts.clear()
        self.repository.close()
        if self._agent_status_disposer is not None:
            self._agent_status_disposer()
            self._agent_status_disposer = None
        self._before_claim = None
        self._hook_runtime = None
        self._session_events = None
        self._agent = None

    async def followup(self, message: InboundMessage | AgentRunRequest) -> IngressResult:
        return await self._admit(message, "next-turn")

    async def steer(self, message: InboundMessage | AgentRunRequest) -> IngressResult:
        return await self._admit(message, "next-step")

    async def inject(self, message: InboundMessage | AgentRunRequest) -> IngressResult:
        return await self._admit(message, "next-step", wake=False)

    async def snapshot(self, session_id: str) -> InboxSnapshot:
        return await self.repository.snapshot(session_id)

    async def resume_pending(self, session_id: str) -> bool:
        """显式唤醒持久 pending；服务启动不会自动派发。"""
        if self._closed:
            return False
        state = self._state(session_id)
        snapshot = await self.repository.snapshot(session_id)
        if not snapshot.has_pending:
            return False
        state.blocked_reason = None
        self._ensure_worker(session_id)
        state.wake.set()
        return True

    async def delete_session(self, session_id: str) -> None:
        state = self._sessions.pop(session_id, None)
        task = self._workers.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for key, future in tuple(self._receipts.items()):
            if key[0] == session_id:
                if not future.done():
                    future.cancel()
                self._receipts.pop(key, None)
        del state
        await self.repository.delete_session(session_id)

    async def handle_bus_message(self, message: BusMessage) -> IngressResult:
        session_id = str(message.data.get("session_id") or message.from_session)
        request_id = str(message.metadata.request_id or message.id)
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
            request_id=request_id,
            channel_id=str(message.from_channel),
            content=_content_text(message.data.get("content")),
            attachments=tuple(dict(item) for item in (message.data.get("attachments") or ())),
            source=str(message.data.get("source") or "user"),
            metadata=message.metadata.model_dump(mode="json"),
        )
        mode = str(message.data.get("mode") or "queue")
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
        self._state(session_id).wake.set()
        return True

    async def remove(self, session_id: str, request_id: str) -> bool:
        item = await self.repository.remove(session_id, request_id)
        if item is None:
            return False
        await self._publish(session_id)
        self._state(session_id).wake.set()
        return True

    async def promote(self, session_id: str, request_id: str) -> bool:
        item = await self.repository.promote(
            session_id,
            request_id,
            target_run_id=self._active_run_id(session_id, "default"),
        )
        if item is None:
            return False
        await self._publish(session_id)
        self._state(session_id).wake.set()
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
        state = self._state(session_id)
        while True:
            snapshot = await self.repository.snapshot(session_id)
            busy = self._agent is not None and self._agent.is_busy(session_id)
            if not snapshot.has_pending and not busy:
                return {"session_id": session_id, "status": "quiescent"}
            state.wake.clear()
            snapshot = await self.repository.snapshot(session_id)
            busy = self._agent is not None and self._agent.is_busy(session_id)
            if not snapshot.has_pending and not busy:
                return {"session_id": session_id, "status": "quiescent"}
            await state.wake.wait()

    async def claim_next_step_for_reasoning(self, session_id: str) -> tuple[QueueItem, ...]:
        return await self.deliver_next_step_for_reasoning(session_id)

    async def deliver_next_step_for_reasoning(
        self,
        session_id: str,
        *,
        turn_id: str = "",
    ) -> tuple[QueueItem, ...]:
        if self._closed:
            return ()
        snapshot = await self.repository.snapshot(session_id)
        candidates = tuple(
            item
            for item in snapshot.next_step
            if item.target_run_id is None
            or (bool(turn_id) and item.target_run_id == turn_id)
        )
        if not candidates:
            return ()
        decision, discarded = await self._before_claim_batch(session_id, snapshot, candidates)
        if decision == "keep":
            return ()
        if decision == "discard":
            await self._discard(session_id, discarded, "before-claim-discard")
            return ()
        history_ids = await self._persist_user_messages(candidates, run_id=turn_id)
        claimed = await self.repository.claim(
            session_id,
            tuple(item.request_id for item in candidates),
        )
        if not claimed:
            return ()
        await self._publish(session_id)
        return self._attach_history_ids(claimed, history_ids)

    async def _admit(
        self,
        message: InboundMessage | AgentRunRequest,
        target: QueueTarget,
        *,
        wake: bool = True,
    ) -> IngressResult:
        if self._closed:
            return IngressResult(False, message.session_id, message.request_id, False, error={
                "code": "inbox-closed", "message": "Inbox 已关闭",
            })
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
            return IngressResult(False, item.session_id, item.request_id, False, error={
                "code": "queue-full", "message": str(exc), "retryable": True,
            })
        except ValueError as exc:
            return IngressResult(False, item.session_id, item.request_id, False, error={
                "code": "session-not-found", "message": str(exc), "retryable": False,
            })

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
        state = self._state(item.session_id)
        if wake:
            if state.blocked_reason is not None and state.blocked_reason.startswith(("before-claim", "claim:")):
                state.blocked_reason = None
            self._ensure_worker(item.session_id)
            state.wake.set()
        return IngressResult(True, item.session_id, item.request_id, created)

    def _item_from_message(
        self,
        message: InboundMessage | AgentRunRequest,
        target: QueueTarget,
    ) -> QueueItem:
        metadata = dict(message.metadata or {})
        agent_id = str(metadata.get("agent_id") or "default")
        target_run_id = None
        if target == "next-step":
            target_run_id = str(metadata.get("target_run_id") or "") or None
            if target_run_id is None:
                target_run_id = self._active_run_id(message.session_id, agent_id)
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
            target_run_id=target_run_id,
        )

    def _state(self, session_id: str) -> _SessionState:
        return self._sessions.setdefault(session_id, _SessionState())

    def _ensure_worker(self, session_id: str) -> None:
        if self._closed or self._agent is None:
            return
        task = self._workers.get(session_id)
        if task is None or task.done():
            task = asyncio.create_task(self._worker(session_id), name=f"inbox:{session_id}")
            self._workers[session_id] = task
            self._state(session_id).worker = task

    async def _worker(self, session_id: str) -> None:
        state = self._state(session_id)
        try:
            while not self._closed:
                state.wake.clear()
                snapshot = await self.repository.snapshot(session_id)
                candidates = self._candidate_batch(snapshot)
                if not candidates:
                    return
                if self._gated(state) or not self._agent_can_receive(session_id):
                    return
                decision, discarded = await self._before_claim_batch(
                    session_id, snapshot, candidates,
                )
                if decision == "keep":
                    state.blocked_reason = "before-claim:rejected"
                    await self._defer(session_id, candidates[0], "before-claim-rejected")
                    return
                if decision == "discard":
                    await self._discard(session_id, discarded, "before-claim-discard")
                    continue
                try:
                    history_ids = await self._persist_user_messages(candidates)
                    claimed = await self.repository.claim(
                        session_id,
                        tuple(item.request_id for item in candidates),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep pending on claim failure
                    state.blocked_reason = f"claim:{exc}"
                    await self._status(session_id, "blocked")
                    return
                if not claimed:
                    continue
                claimed = self._attach_history_ids(claimed, history_ids)
                await self._publish(session_id)
                await self._observe(
                    INBOX_CLAIMED_SPEC,
                    InboxClaimedPayload(
                        session_id=session_id,
                        request_ids=tuple(item.request_id for item in claimed),
                    ),
                )
                completed = await self._deliver(session_id, claimed)
                await self._publish(session_id)
                if completed:
                    state.wake.set()
        except asyncio.CancelledError:
            return
        finally:
            if self._workers.get(session_id) is asyncio.current_task():
                self._workers.pop(session_id, None)
            if state.worker is asyncio.current_task():
                state.worker = None

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
                    await self._status(session_id, "paused")
                    self._resolve(item, result)
                    return False
                if status in {"failed", "cancelled", "interrupted"}:
                    await self._report_failure(session_id, item, reason, retryable)
                    self._state(session_id).blocked_reason = f"agent:{reason or status}"
                    await self._status(session_id, "blocked")
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
                await self._observe(
                    INBOX_ERROR_SPEC,
                    InboxErrorPayload(
                        session_id=session_id,
                        request_id=item.request_id,
                        stage="agent-run",
                        error=str(exc),
                    ),
                )
                await self._observe(
                    INBOX_FAILED_SPEC,
                    InboxFailedPayload(
                        session_id=session_id,
                        request_id=item.request_id,
                        reason=str(exc),
                    ),
                )
                self._resolve_exception(item, exc)
                self._state(session_id).blocked_reason = f"agent:{exc}"
                await self._status(session_id, "blocked")
                logger.exception(
                    "[ftre-inbox] AgentService.run failed session=%s request=%s",
                    session_id,
                    item.request_id,
                )
                return False
        return True

    async def deliver_one(self, session_id: str, item: QueueItem) -> Any:
        """内部测试/宿主使用的单项交付入口；不重新入队。"""
        return await self._deliver(session_id, (item,))

    async def _persist_user_messages(
        self,
        candidates: tuple[QueueItem, ...],
        *,
        run_id: str = "",
    ) -> dict[str, str]:
        if self._session_events is None:
            return {}
        history_ids: dict[str, str] = {}
        previous_assistant_id = None
        active_id = getattr(self._session_events, "active_assistant_message_id", None)
        if callable(active_id):
            previous_assistant_id = await active_id(candidates[0].session_id)
        for candidate in candidates:
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
        self._state(session_id).blocked_reason = None
        self._state(session_id).wake.set()

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

    async def _status(self, session_id: str, status: str) -> None:
        if self._hook_runtime is not None and INBOX_STATUS_CHANGED_SPEC is not None:
            await self._hook_runtime.dispatch(
                INBOX_STATUS_CHANGED_SPEC,
                InboxStatusPayload(session_id=session_id, status=status),
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

    def status(self, session_id: str) -> str | None:
        state = self._state(session_id)
        if state.blocked_reason is not None:
            return "blocked"
        status = getattr(self._agent, "status", None)
        if callable(status):
            try:
                current = str(status(session_id))
            except Exception:  # noqa: BLE001 - status is diagnostic only
                current = ""
            if current == "paused":
                return "paused"
            if current in {"failed", "cancelled", "interrupted"}:
                return "blocked"
        return None

    def _on_agent_status(self, view: Any) -> None:
        session_id = getattr(view, "session_id", None) or getattr(view, "agent_id", None)
        if session_id:
            session_id = str(session_id)
            self._state(session_id).wake.set()
            if str(getattr(view, "state", "")) in {"idle", "completed"}:
                self._ensure_worker(session_id)

    def _agent_can_receive(self, session_id: str) -> bool:
        if self._agent is None:
            return False
        # AgentService 的公开 status(session_id) 接口兼容 Runtime 的
        # session 查询，但一个 Agent 的公开 id 可能是 ``session:profile``。
        # 先检查 AgentView，避免把 paused/等待确认误判成 idle。
        list_agents = getattr(self._agent, "list", None)
        if callable(list_agents):
            try:
                views = tuple(list_agents())
            except Exception:  # noqa: BLE001 - status gate is diagnostic only
                views = ()
            session_views = tuple(
                view for view in views
                if getattr(view, "session_id", None) == session_id
            )
            if session_views:
                current = str(getattr(session_views[0], "state", ""))
                if current in {
                    "running", "processing", "compacting", "paused", "stopping",
                    "failed", "cancelled", "interrupted", "awaiting_confirmation",
                }:
                    return False
                if current:
                    return True
        status = getattr(self._agent, "status", None)
        if callable(status):
            try:
                current = str(status(session_id))
            except Exception:  # noqa: BLE001 - a status probe must not break delivery
                current = ""
            if current in {
                "running", "processing", "compacting", "paused", "stopping",
                "failed", "cancelled", "interrupted", "awaiting_confirmation",
            }:
                return False
            if current:
                return True
        busy = getattr(self._agent, "is_busy", None)
        return not callable(busy) or not busy(session_id)

    def _uses_agent_service(self) -> bool:
        return self._agent is not None and callable(getattr(self._agent, "get", None))

    @staticmethod
    def _candidate_batch(snapshot: InboxSnapshot) -> tuple[QueueItem, ...]:
        next_step = [item for item in snapshot.next_step if item.target_run_id is None]
        if next_step:
            return (*next_step, *snapshot.next_turn[:1])
        return snapshot.next_turn[:1]

    @staticmethod
    def _gated(state: _SessionState) -> bool:
        return state.blocked_reason is not None

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

    def _active_run_id(self, session_id: str, agent_id: str) -> str | None:
        if self._agent is None:
            return None
        get = getattr(self._agent, "get", None)
        if callable(get):
            for candidate in (f"{session_id}:{agent_id}", agent_id, session_id):
                view = get(candidate)
                if self._active_view(view, session_id):
                    return str(view.run_id)
        list_agents = getattr(self._agent, "list", None)
        if callable(list_agents):
            for view in list_agents():
                if self._active_view(view, session_id):
                    return str(view.run_id)
        return None

    @staticmethod
    def _active_view(view: Any, session_id: str) -> bool:
        return (
            view is not None
            and getattr(view, "session_id", None) == session_id
            and str(getattr(view, "state", ""))
            in {"running", "processing", "compacting", "paused", "stopping"}
            and bool(getattr(view, "run_id", None))
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

    def _resolve(self, item: QueueItem, result: Any) -> None:
        future = self._receipts.pop((item.session_id, item.request_id), None)
        if future is not None and not future.done():
            future.set_result(result)

    def _resolve_exception(self, item: QueueItem, error: Exception) -> None:
        future = self._receipts.pop((item.session_id, item.request_id), None)
        if future is not None and not future.done():
            future.set_exception(error)


__all__ = ["InboxService"]
