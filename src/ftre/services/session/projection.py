"""SessionProjection：Gateway 持有的会话 Event → Msg 投影层。

AgentScope Event 是实时传输过程；进行中的 assistant Reply 则是由一条可变
Msg 快照表达的会话事实。本模块是 Event 聚合、checkpoint 与 attach 快照的唯一
所有者。

它是运行时投影组件而非独立 Service：SessionService 负责最终持久化，Projection
负责把 stream 事件聚合成可恢复的 Msg 快照；两者之间通过显式绑定协作。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ftre_agent.event import (
    ReplyEndEvent,
    ReplyFinishedReason,
    ReplyStartEvent,
    UserMessageEvent,
)
from ftre_agent.message import AssistantMsg, Msg, MsgName, UserMsg

from ftre.services.session.events import (
    SessionMaintenanceEvent,
    SessionMaintenanceRecord,
)

if TYPE_CHECKING:
    from ftre.services.session import SessionService

logger = logging.getLogger(__name__)

# 连续 TEXT_BLOCK_DELTA 等高频事件最多每 0.5 秒写一次 state.json。
# 这样既限制进程异常时可能丢失的窗口，又不会每生成一个 token 都触发磁盘 I/O。
# 以下 Event 表示一个可独立恢复的语义边界，收到后必须立即 checkpoint：
# - REPLY_START：先持久化空 assistant Msg，避免回复身份在进程退出后消失；
# - *_BLOCK_END：正文、思考或数据块已完整，可安全恢复；
# - TOOL_CALL_* / TOOL_RESULT_END：工具意图、完整参数或完整结果不能只留在内存；
# - MODEL_CALL_END：本次模型调用已结束，usage 等统计字段稳定。
# REPLY_END 不在这里：它走 apply() 中独立的最终写入和 active Reply 移除逻辑。
IMMEDIATE_CHECKPOINT_TYPES = frozenset({
    "REPLY_START", "TEXT_BLOCK_END", "THINKING_BLOCK_END",
    "TOOL_CALL_START", "TOOL_CALL_END", "TOOL_RESULT_END", "MODEL_CALL_END",
    # 权限确认：把 tool_call 置 ASKING 后必须立即落盘。挂起不产 REPLY_END，
    # 若不在此 checkpoint，ASKING 只停留在内存，进程/实例销毁后无法从 state.json 恢复。
    # 用户决定同样必须立即落盘，否则同批多个 ASK 在逐个确认、新建 Agent 时
    # 会丢失前一次 ALLOWED/FINISHED 状态。
    "REQUIRE_USER_CONFIRM", "USER_CONFIRM_RESULT",
})


@dataclass
class ProjectionResult:
    """apply() 的返回值：本次投影写入/更新的 Msg 及可选的 completed Msg。

    - ``persisted_messages``：本次投影新写入或更新的 Msg（如 compact 摘要 Msg）。
    - ``completed_message``：REPLY_END 时完成的 assistant Msg（供调用方取正文）。
    """
    persisted_messages: list[Msg] = field(default_factory=list)
    completed_message: Msg | None = None


@dataclass
class ReplyState:
    """一条正在生成的 assistant Reply 的内存状态。

    ``message`` 是恢复与最终持久化的事实来源；Event 只在抵达时用于修改它，
    不会被保存在这里。客户端 attach 时拿到的是 ``message + revision``。
    """
    session_id: str
    reply_id: str
    message_id: str
    message: Msg                  # Event 聚合出的完整 Msg 快照
    revision: int = 0             # 每接收一个 Event 增加，用于识别快照新旧
    dirty: bool = False           # 内存 Msg 是否比磁盘中的 checkpoint 更新


class SessionProjection:
    """将流式 Event 投影为运行中 Msg 快照，并维护其持久化状态。"""

    def __init__(self, session_manager: SessionService) -> None:
        # SessionManager 是唯一的落盘入口；Projection 不直接读写 state.json。
        self._session_manager = session_manager
        # 仅保存尚未 REPLY_END 的 AssistantMsg：
        # session_id → message_id → ReplyState。reply_id 仍用于关联整次 run。
        self._replies: dict[str, dict[str, ReplyState]] = {}
        # 不落盘的 session 级运行状态。它们不是 Msg，也没有 reply_id，但客户端
        # attach 时必须知道（例如正在进行的 context compact）。终态到达即清除。
        self._active_session_events: dict[
            str, dict[SessionMaintenanceEvent, SessionMaintenanceRecord]
        ] = {}
        # 保护上述内存投影，避免 Event 流、attach snapshot 与取消路径并发修改同一 Msg。
        # 普通 Event 的磁盘 I/O 在锁外执行；UserMessage 已由 Inbox 在安全边界提交，
        # Projection 只按 message_id 更新对应 AssistantMsg。
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """Drop in-memory Reply/maintenance state during Session shutdown."""
        async with self._lock:
            self._replies.clear()
            self._active_session_events.clear()

    async def apply(self, session_id: str, event) -> ProjectionResult:
        """应用一个 Event，返回投影结果。

        处理两类事件：
        - Reply 生命周期事件（REPLY_START/.../REPLY_END）：聚合为运行中 assistant
          Msg 快照，REPLY_END 时返回 completed_message。
        - Host maintenance events are handled by ``apply_maintenance`` and never
          enter the Agent event union.

        Msg 只在这里创建和修改，Turn 与 WebSocket 均不持有它。每个成功接收的
        Reply 事件都会推进内存快照的 revision。
        """
        if isinstance(event, UserMessageEvent):
            previous_id = str(
                event.message_metadata.get("previous_assistant_message_id")
                or event.data.get("previous_assistant_message_id")
                or ""
            )
            if previous_id:
                await self._close_assistant_at_user_boundary(
                    session_id,
                    previous_id,
                    finished_at=event.created_at,
                )
            message = UserMsg(
                name=MsgName.DEFAULT,
                content=event.content,
                id=event.id,
                created_at=event.created_at,
                metadata=event.message_metadata,
            )
            await self._session_manager.upsert_message(session_id, message)
            return ProjectionResult(persisted_messages=[message])

        reply_id = getattr(event, "reply_id", "") or ""
        if not reply_id:
            return ProjectionResult()

        event_type = getattr(event, "type", "")
        message_id = getattr(event, "message_id", None) or reply_id
        is_start = isinstance(event, ReplyStartEvent)
        is_end = isinstance(event, ReplyEndEvent)
        save_new = False
        update_snapshot: Msg | None = None
        completed: Msg | None = None
        restored_message: Msg | None = None

        # Gateway 重启后内存投影为空，但暂停中的 Reply 已经 checkpoint 到磁盘。
        # 恢复事件不能新建同 id 的空 Msg（save_message 会冲突），应先复用持久化快照。
        if not is_start:
            async with self._lock:
                has_active = message_id in self._replies.get(session_id, {})
            if not has_active:
                from ftre.services.session.message.converter import _as_msg

                records = await self._session_manager.get_messages_by_session(
                    session_id
                )
                for record in records:
                    record_id = (
                        record.id if isinstance(record, Msg) else record.get("id")
                    )
                    if record_id == message_id:
                        restored_message = _as_msg(record)
                        break

        # 终态 Assistant 不能再接收恢复/重放事件；否则旧文本会被继续追加，
        # 重启后的重复 Resume 就会污染原始消息。暂停中的未终态 Reply 仍允许
        # confirmation 路径复用，这是唯一保留的跨进程恢复语义。
        if restored_message is not None and restored_message.finished_at is not None:
            return ProjectionResult()
        if is_start and restored_message is not None:
            return ProjectionResult()

        boundary_updates: list[Msg] = []
        async with self._lock:
            # 同一 session 的 Event 可能来自异步流与取消路径；锁只保护内存投影，
            # 文件 I/O 在锁外进行，避免一个慢磁盘阻塞所有 session。
            replies = self._replies.setdefault(session_id, {})
            state = replies.get(message_id)
            if state is None:
                # Runtime 在同一次 reply 中开启新的 message_id 时，上一条 Assistant
                # 已经完成安全边界；先封口，稍后在锁外 checkpoint。
                for previous_id, previous in tuple(replies.items()):
                    if previous.reply_id != reply_id or previous_id == message_id:
                        continue
                    if previous.message.finished_at is None:
                        previous.message.finished_at = getattr(event, "created_at", None)
                        previous.revision += 1
                        previous.dirty = False
                        boundary_updates.append(previous.message.model_copy(deep=True))
                    replies.pop(previous_id, None)
                if restored_message is not None:
                    message = restored_message
                else:
                    metadata = dict(getattr(event, "metadata", {}) or {})
                    if is_start and getattr(event, "name", ""):
                        # Msg.name 只表示消息语义；实际调用的模型属于可选元数据。
                        metadata["model"] = event.name
                    message = AssistantMsg(
                        name=MsgName.DEFAULT,
                        content=[], id=message_id,
                        created_at=getattr(event, "created_at", None),
                        metadata=metadata,
                    )
                state = ReplyState(session_id, reply_id, message_id, message)
                replies[message_id] = state
                # 即便流异常地缺少 REPLY_START，也必须能生成可持久化 Msg。
                save_new = restored_message is None

            # 少数异常流会先到达 reply 事件、后到 REPLY_START；仍在此补齐模型元数据。
            if is_start and getattr(event, "name", ""):
                state.message.metadata["model"] = event.name

            state.message.append_event(event)
            state.revision += 1

            if is_start:
                # 空 Msg 在 ReplyStart 立即入库：即使进程随后退出，也能找到该 Reply。
                save_new = True
                state.dirty = False
            elif is_end:
                # 最终 Event 已将结束信息写入 Msg。先保留 active 状态，等下面的
                # update_message 成功后再移除；这样取消或 Session 删除不能在落盘
                # 前把唯一的内存快照丢掉，finish_open 仍能完成兜底收尾。
                state.dirty = False
                completed = state.message
            else:
                state.dirty = True
                if event_type in IMMEDIATE_CHECKPOINT_TYPES:
                    # 语义边界（文本块/工具调用结束等）立即成为新的持久化快照。
                    update_snapshot = self._mark_checkpointed(state)

        # 这里开始不再持有内存锁。每次写的都是完整 Msg，而不是 Event 列表。
        for boundary in boundary_updates:
            await self._session_manager.update_message(boundary)
        if save_new:
            await self._session_manager.save_message(session_id, state.message)
        if update_snapshot is not None:
            await self._session_manager.update_message(update_snapshot)
        if completed is not None:
            # 持有投影锁直到最终快照提交成功，避免 finish_open 或并发 Event 在
            # "已标记完成、尚未落盘" 的窗口中移除/覆盖同一 Reply。
            async with self._lock:
                await self._session_manager.update_message(completed)
                replies = self._replies.get(session_id)
                if replies is not None and replies.get(message_id) is state:
                    replies.pop(message_id, None)
                    if not replies:
                        self._replies.pop(session_id, None)
        return ProjectionResult(completed_message=completed)

    async def apply_maintenance(
        self, session_id: str, event: SessionMaintenanceRecord
    ) -> ProjectionResult:
        """Apply a typed Host maintenance record outside AgentStreamEvent."""
        if event.name == SessionMaintenanceEvent.COMPACTION_START:
            async with self._lock:
                self._active_session_events.setdefault(session_id, {})[
                    SessionMaintenanceEvent.COMPACTION_START
                ] = event
            return ProjectionResult()
        if event.name == SessionMaintenanceEvent.COMPACTION_DONE:
            result = await self._project_compact_done(session_id, event)
            await self._clear_active_session_event(
                session_id, SessionMaintenanceEvent.COMPACTION_START
            )
            return result
        if event.name == SessionMaintenanceEvent.COMPACTION_FAILED:
            await self._clear_active_session_event(
                session_id, SessionMaintenanceEvent.COMPACTION_START
            )
        return ProjectionResult()

    async def _project_compact_done(
        self, session_id: str, event: SessionMaintenanceRecord
    ) -> ProjectionResult:
        """把 context_compact_done 投影为一条 Msg 并幂等落盘。

        - summary 模式：投影为 name=compact 的上下文锚点 Msg（正文为完整摘要）。
        - fast 模式：投影为 name=compact_fast 的展示气泡 Msg（正文为提示文案，
          告知工具输出已被裁剪）。该 Msg 不是上下文锚点，不设 through_message_id，
          不参与 tail 计算，仅供前端展示与提醒 Agent。
        event.id 作为 Msg id，保证同一事件重放不会产生重复 Msg。
        """
        value = event.value or {}
        mode = value.get("mode")

        if mode == "fast":
            tool_results = int(value.get("tool_results", 0) or 0)
            tokens_before = int(value.get("tokens_before", 0) or 0)
            tokens_after = int(value.get("tokens_after", 0) or 0)
            saved = max(0, tokens_before - tokens_after)
            text = (
                f"已快速压缩：{tool_results} 个较早的工具输出已被裁剪，"
                f"其原始内容不再可见（约节省 {saved} tokens）。"
                "后续如需相关信息请重新获取。"
            )
            # 用 assistant 角色：语义上是「助手自述裁剪了工具输出」，且不会被
            # compress_fast 的 user-turn 计数误当成一轮（role != user）。
            # 前端按 name=compact_fast 专属分支渲染成气泡，无需 hide 标记。
            message = AssistantMsg(
                name=MsgName.COMPACT_FAST,
                content=text,
                id=event.id,
                created_at=event.created_at,
                metadata={
                    "context_compact": {
                        "mode": "fast",
                        "tool_results": tool_results,
                        "tokens_before": tokens_before,
                        "tokens_after": tokens_after,
                    },
                },
            )
            await self._session_manager.upsert_message(session_id, message)
            return ProjectionResult(persisted_messages=[message])

        if mode != "summary":
            return ProjectionResult()
        summary_text = value.get("summary_text") or ""
        compact_meta = {
            "through_message_id": value.get("through_message_id", ""),
            "trigger": value.get("trigger", "auto"),
            "tokens_before": value.get("tokens_before", 0),
            "tokens_after": value.get("tokens_after", 0),
        }
        message = UserMsg(
            name=MsgName.COMPACT,
            content=summary_text,
            id=event.id,
            created_at=event.created_at,
            metadata={"hide": True, "context_compact": compact_meta},
        )
        await self._session_manager.upsert_message(session_id, message)
        return ProjectionResult(persisted_messages=[message])

    async def finish_open(
        self, session_id: str, reason: ReplyFinishedReason, *, error: dict | None = None,
    ) -> list[Msg]:
        """结束某个失败/取消 Turn 的全部 open Reply，并持久化终态。"""
        completed: list[Msg] = []
        async with self._lock:
            replies = self._replies.pop(session_id, {})
            for state in replies.values():
                state.message.finished_at = datetime.now().isoformat()  # noqa: DTZ005 legacy compatibility boundary reviewed in F1
                state.message.finished_reason = reason
                state.message.error = error
                state.revision += 1
                state.dirty = False
                completed.append(state.message)
        for message in completed:
            await self._session_manager.update_message(message)
        return completed

    async def _close_assistant_at_user_boundary(
        self,
        session_id: str,
        message_id: str,
        *,
        finished_at: str | None,
    ) -> None:
        """在真实 UserMessage 到达时立即封口上一条 Assistant。"""
        boundary: Msg | None = None
        active_state: ReplyState | None = None
        async with self._lock:
            replies = self._replies.get(session_id, {})
            state = replies.get(message_id)
            if state is not None:
                active_state = state
                state.message.finished_at = finished_at or datetime.now(UTC).isoformat()
                state.message.finished_reason = ReplyFinishedReason.COMPLETED
                state.revision += 1
                state.dirty = False
                boundary = state.message.model_copy(deep=True)

        if boundary is None:
            from ftre.services.session.message.converter import _as_msg

            records = await self._session_manager.get_messages_by_session(session_id)
            for record in records:
                record_id = record.id if isinstance(record, Msg) else record.get("id")
                if record_id != message_id:
                    continue
                candidate = _as_msg(record)
                if candidate.role != "assistant" or candidate.finished_at is not None:
                    return
                candidate.finished_at = finished_at or datetime.now(UTC).isoformat()
                candidate.finished_reason = ReplyFinishedReason.COMPLETED
                boundary = candidate
                break

        if boundary is not None:
            await self._session_manager.update_message(boundary)
            if active_state is not None:
                async with self._lock:
                    replies = self._replies.get(session_id)
                    if replies is not None and replies.get(message_id) is active_state:
                        replies.pop(message_id, None)
                        if not replies:
                            self._replies.pop(session_id, None)

    async def snapshot(self, session_id: str) -> list[dict]:
        """返回一个 session 内每条运行中 AssistantMsg 的最新完整快照。"""
        async with self._lock:
            return [
                {
                    "reply_id": state.reply_id,
                    "message_id": state.message_id,
                    "revision": state.revision,
                    "message": state.message.model_dump(mode="json"),
                }
                for state in self._replies.get(session_id, {}).values()
            ]

    async def session_event_snapshot(self, session_id: str) -> list[dict]:
        """返回仅驻内存、仍处于 active 状态的 session 级 Event。"""
        async with self._lock:
            return [
                event.model_dump(mode="json")
                for event in self._active_session_events.get(session_id, {}).values()
            ]

    async def _clear_active_session_event(
        self, session_id: str, key: SessionMaintenanceEvent
    ) -> None:
        async with self._lock:
            events = self._active_session_events.get(session_id)
            if not events:
                return
            events.pop(key, None)
            if not events:
                self._active_session_events.pop(session_id, None)

    def _mark_checkpointed(self, state: ReplyState) -> Msg:
        """标记内存与磁盘将同步；返回要在锁外写入的完整 Msg。"""
        state.dirty = False
        return state.message
