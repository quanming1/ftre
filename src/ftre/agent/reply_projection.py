"""ReplyProjection：Gateway 持有的 Event → Msg 投影层。

AgentScope Event 是实时传输过程；进行中的 assistant Reply 则是由一条可变
Msg 快照表达的会话事实。本模块是 Event 聚合、checkpoint 与 attach 快照的唯一
所有者。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ftre_agent_core.event import ReplyEndEvent, ReplyFinishedReason, ReplyStartEvent
from ftre_agent_core.message import AssistantMsg, Msg

if TYPE_CHECKING:
    from ftre.session import SessionManager

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
    "REPLY_START", "TEXT_BLOCK_END", "THINKING_BLOCK_END", "DATA_BLOCK_END",
    "TOOL_CALL_START", "TOOL_CALL_END", "TOOL_RESULT_END", "MODEL_CALL_END",
})


@dataclass
class ReplyState:
    """一条正在生成的 assistant Reply 的内存状态。

    ``message`` 是恢复与最终持久化的事实来源；Event 只在抵达时用于修改它，
    不会被保存在这里。客户端 attach 时拿到的是 ``message + revision``。
    """
    session_id: str
    reply_id: str
    message: Msg                  # Event 聚合出的完整 Msg 快照
    revision: int = 0             # 每接收一个 Event 增加，用于识别快照新旧
    dirty: bool = False           # 内存 Msg 是否比磁盘中的 checkpoint 更新


class ReplyProjection:
    """将流式 Event 投影为运行中 Msg 快照，并维护其持久化状态。"""

    def __init__(self, session_manager: SessionManager) -> None:
        # SessionManager 是唯一的落盘入口；Projection 不直接读写 state.json。
        self._session_manager = session_manager
        # 仅保存尚未 REPLY_END 的运行中 Reply：
        # session_id → reply_id → ReplyState。完成后会立即从这里移除。
        self._replies: dict[str, dict[str, ReplyState]] = {}
        # 保护上述内存投影，避免 Event 流、attach snapshot 与取消路径并发修改同一 Reply。
        # 锁内只改内存；SessionManager 的磁盘 I/O 一律在锁外执行。
        self._lock = asyncio.Lock()

    async def apply(self, session_id: str, event) -> Msg | None:
        """应用一个 Event；若为 ``REPLY_END`` 则返回已完成的 Msg。

        Msg 只在这里创建和修改，Turn 与 WebSocket 均不持有它。每个成功接收的
        Event 都会推进内存快照的 revision。
        """
        reply_id = getattr(event, "reply_id", "") or ""
        if not reply_id:
            # 非 Reply 生命周期事件（如 TURN_START）没有对应 Msg，只负责实时转发。
            return None

        event_type = getattr(event, "type", "")
        is_start = isinstance(event, ReplyStartEvent)
        is_end = isinstance(event, ReplyEndEvent)
        save_new = False
        update_snapshot: Msg | None = None
        completed: Msg | None = None

        async with self._lock:
            # 同一 session 的 Event 可能来自异步流与取消路径；锁只保护内存投影，
            # 文件 I/O 在锁外进行，避免一个慢磁盘阻塞所有 session。
            replies = self._replies.setdefault(session_id, {})
            state = replies.get(reply_id)
            if state is None:
                message = AssistantMsg(
                    name=getattr(event, "name", "") or "assistant",
                    content=[], id=reply_id, created_at=getattr(event, "created_at", None),
                )
                state = ReplyState(session_id, reply_id, message)
                replies[reply_id] = state
                # 即便流异常地缺少 REPLY_START，也必须能生成可持久化 Msg。
                save_new = True

            state.message.append_event(event)
            state.revision += 1

            if is_start:
                # 空 Msg 在 ReplyStart 立即入库：即使进程随后退出，也能找到该 Reply。
                save_new = True
                state.dirty = False
            elif is_end:
                # 最终 Event 已将结束信息写入 Msg；先从 active 集合移除，再写终态。
                # 此后 attach 从正常历史读取，不再收到 running snapshot。
                state.dirty = False
                completed = state.message
                replies.pop(reply_id, None)
                if not replies:
                    self._replies.pop(session_id, None)
            else:
                state.dirty = True
                if event_type in IMMEDIATE_CHECKPOINT_TYPES:
                    # 语义边界（文本块/工具调用结束等）立即成为新的持久化快照。
                    update_snapshot = self._mark_checkpointed(state)

        # 这里开始不再持有内存锁。每次写的都是完整 Msg，而不是 Event 列表。
        if save_new:
            await self._session_manager.save_message(session_id, state.message)
        if update_snapshot is not None:
            await self._session_manager.update_message(update_snapshot)
        if completed is not None:
            await self._session_manager.update_message(completed)
        return completed

    async def finish_open(
        self, session_id: str, reason: ReplyFinishedReason, *, error: dict | None = None,
    ) -> list[Msg]:
        """结束某个失败/取消 Turn 的全部 open Reply，并持久化终态。"""
        completed: list[Msg] = []
        async with self._lock:
            replies = self._replies.pop(session_id, {})
            for state in replies.values():
                state.message.finished_at = datetime.now().isoformat()
                state.message.finished_reason = reason
                state.message.error = error
                state.revision += 1
                state.dirty = False
                completed.append(state.message)
        for message in completed:
            await self._session_manager.update_message(message)
        return completed

    async def snapshot(self, session_id: str) -> list[dict]:
        """返回一个 session 内每条运行中 Reply 的最新完整 Msg 快照。"""
        async with self._lock:
            return [
                {"reply_id": state.reply_id, "revision": state.revision,
                 "message": state.message.model_dump(mode="json")}
                for state in self._replies.get(session_id, {}).values()
            ]

    def _mark_checkpointed(self, state: ReplyState) -> Msg:
        """标记内存与磁盘将同步；返回要在锁外写入的完整 Msg。"""
        state.dirty = False
        return state.message
