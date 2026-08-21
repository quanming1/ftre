"""SessionLane 使用的 Mailbox 存储门面。

这一层刻意很薄：它不决定何时执行，也不发布事件，只把 Lane 的语义操作翻译成
SessionManager 的原子持久化操作。这样执行调度与 JSON 状态格式彼此隔离。
"""
from __future__ import annotations

from ftre.services.messaging.bus import BusMessage
from ftre.services.session.entity.state import MailboxState, QueueItem
from ftre.services.session.service import RequestAdmission, SessionService


class MailboxStore:
    """每个 session 的持久 pending 队列唯一读写入口。

    active Turn 与完成等待都是 SessionLane 的进程内职责，不能写进这里。
    """

    def __init__(self, session_manager: SessionService, *, capacity: int = 100) -> None:
        self._sessions = session_manager
        self.capacity = max(1, capacity)

    async def admit(self, inbound: BusMessage) -> RequestAdmission:
        """耐久接纳；返回后才允许调用方回复 accepted。"""
        # Store 只做持久化边界适配，不启动 worker、不发布事件；调度策略统一留在 SessionLane。
        return await self._sessions.admit_inbound(
            inbound, mailbox_capacity=self.capacity
        )

    async def peek(self, session_id: str) -> QueueItem | None:
        return await self._sessions.peek_request(session_id)

    async def take(self, session_id: str, request_id: str) -> QueueItem | None:
        """从 pending 原子取走队首；后续 active 只由 SessionLane 在内存保存。"""
        return await self._sessions.take_pending_request(session_id, request_id)

    async def cancel_pending(self, session_id: str, request_id: str) -> QueueItem | None:
        return await self._sessions.cancel_pending_request(session_id, request_id)

    async def snapshot(self, session_id: str) -> MailboxState:
        # 返回只读语义的深拷贝，调用方不能绕过 Repository 锁直接修改状态。
        return await self._sessions.get_mailbox_snapshot(session_id)

    async def advance_revision(self, session_id: str) -> int:
        """推进客户端 mailbox 快照版本，不持久化 active 或完成结果。"""
        return await self._sessions.advance_mailbox_revision(session_id)

    async def recoverable_sessions(self) -> list[str]:
        return await self._sessions.get_mailbox_session_ids()

    async def channel_id(self, session_id: str) -> str:
        """读取会话的权威执行 Channel，QueueItem 不重复存路由字段。"""
        session = await self._sessions.get_session(session_id)
        return str(session["channel_id"]) if session is not None else ""
