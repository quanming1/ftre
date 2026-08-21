"""SessionRepository —— Session 纯数据存取（CRUD + 索引 + 提交）。

只负责把 AgentStateFile 搬进搬出并维护索引，不含任何业务规则
（上下文裁剪 / token 计算 / 前端投影等归 Service 层）。

并发模型：per-session asyncio.Lock + 全局 create/delete 锁；
写盘采用临时文件 + fsync + os.replace 原子替换，写盘成功后才提交内存缓存。

会话数据只从 ``sessions/`` 目录中的当前 JSON 模型读取，不提供旧格式迁移。
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ftre_agent_core.message import Msg, MsgName

from ftre.config import CONFIG_PATH
from ftre.services.session.entity.models import (
    ExternalSessionModel,
    MessageModel,
    SessionModel,
)
from ftre.services.session.entity.state import (
    AgentStateFile,
    MailboxState,
    QueueItem,
    SessionState,
)

from .json_store import JsonStateStore, validate_session_id

logger = logging.getLogger(__name__)


# 该参数保留为构造函数的目录锚点；实际持久化始终使用 sessions/ JSON 文件。
DEFAULT_DB_PATH = str(CONFIG_PATH.parent / "sessions.db")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _epoch_to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat()


def _iso_to_epoch(value: str | None) -> float:
    try:
        return datetime.fromisoformat(value or "").timestamp()
    except ValueError:
        return time.time()


# channel_id 只允许字母、数字、下划线、连字符（保证拼接后的 session_id 可安全作目录名）
_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_channel_id(channel_id: str) -> None:
    if not channel_id:
        raise ValueError("channel_id 不能为空")
    if not _CHANNEL_ID_RE.match(channel_id):
        raise ValueError(
            f"channel_id 含非法字符（只允许 [A-Za-z0-9_-]）: {channel_id!r}"
        )


# 会话列表预览：最后一条真实用户消息文本的最大长度（字符）
_LAST_USER_TEXT_MAX = 200


def _last_user_text(state: AgentStateFile) -> str:
    """提取最后一条真实用户消息的文本摘要（倒序找第一条命中的）。

    "真实用户消息"判定：role == user 且 name == default（跳过 compact/compact_fast
    摘要，它们虽 role=user 但是系统生成的）。取该消息全部 TextBlock 文本，
    折叠空白、截断到 _LAST_USER_TEXT_MAX。无命中返回空串。
    """
    for msg in reversed(state.messages):
        if msg.role != "user":
            continue
        if msg.name != MsgName.DEFAULT.value:
            continue
        if msg.metadata.get("hide"):
            continue
        texts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        joined = " ".join(t.strip() for t in texts if t and t.strip())
        joined = re.sub(r"\s+", " ", joined).strip()
        if not joined:
            continue
        return joined[:_LAST_USER_TEXT_MAX]
    return ""


class SessionRepository:
    """Session 数据存取唯一入口；调用方不应直接读写 state.json。"""

    def __init__(self, db_path: str | None = None, *, sessions_dir: str | None = None):
        # db_path 仅用于推导 sessions/ 所在的配置目录。
        self._db_path = db_path or DEFAULT_DB_PATH
        root = Path(sessions_dir) if sessions_dir else Path(self._db_path).parent / "sessions"
        self._sessions_root = root
        self._store = JsonStateStore(root)
        self._states = self._store.states  # 引用同一 dict（store 负责清空/填充）
        # Msg.id → session_id（Msg.id 在配置目录内全局唯一）
        self._message_sessions: dict[str, str] = {}
        # (channel_id, external_key) → session_id
        self._external_sessions: dict[tuple[str, str], str] = {}

    async def init(self) -> None:
        """启动时加载当前 JSON 状态并重建索引。"""
        await self._store.load_all()
        self._rebuild_indexes()

    def create_id(self) -> str:
        """生成新的 session_id"""
        return f"sess_{uuid.uuid4().hex[:12]}"

    # ============================================================
    # 供 Service 层使用的数据访问原语
    # ============================================================

    def get_state(self, session_id: str) -> AgentStateFile | None:
        """读取内存中的完整状态；不存在返回 None（损坏 session 明确报错）。"""
        state = self._states.get(session_id)
        if state is None:
            self._ensure_not_corrupt(session_id)
            return None
        return state

    def all_states(self) -> list[tuple[str, AgentStateFile]]:
        """全部 (session_id, 状态) 快照列表，供启动期全量扫描类业务使用。"""
        return list(self._states.items())

    def lock_for(self, session_id: str) -> asyncio.Lock:
        """一个 Session 一把锁；读写同一 session 的状态须持有。"""
        return self._store.lock_for(session_id)

    @property
    def global_lock(self) -> asyncio.Lock:
        return self._store.global_lock

    def state_path(self, session_id: str) -> Path:
        return self._store.state_path(session_id)

    def sessions_root(self) -> Path:
        """sessions 存储根目录（~/.ftre/sessions/）。"""
        return self._store.root

    async def commit(self, new_state: AgentStateFile) -> None:
        """原子写盘成功后提交内存缓存（并发场景调用方必须已持有对应锁）。"""
        await self._store.write(new_state)
        session_id = new_state.session.id
        old_state = self._states.get(session_id)
        if old_state is not None:
            for message in old_state.messages:
                self._message_sessions.pop(message.id, None)
        self._states[session_id] = new_state
        for message in new_state.messages:
            self._message_sessions[message.id] = session_id
        external = self._external_of(new_state)
        stale = [k for k, v in self._external_sessions.items() if v == session_id]
        for key in stale:
            self._external_sessions.pop(key, None)
        if external is not None:
            key = (external["channel_id"], external["external_key"])
            self._external_sessions[key] = session_id

    # ============================================================
    # 内部：索引 / 边界转换
    # ============================================================

    def _rebuild_indexes(self) -> None:
        self._message_sessions.clear()
        self._external_sessions.clear()
        for session_id, state in self._states.items():
            for message in state.messages:
                self._message_sessions[message.id] = session_id
            external = self._external_of(state)
            if external is not None:
                key = (external["channel_id"], external["external_key"])
                self._external_sessions[key] = session_id

    @staticmethod
    def _external_of(state: AgentStateFile) -> dict[str, Any] | None:
        external = state.metadata.get("external")
        if (
            isinstance(external, dict)
            and isinstance(external.get("channel_id"), str)
            and isinstance(external.get("external_key"), str)
        ):
            return external
        return None

    def _ensure_not_corrupt(self, session_id: str) -> None:
        """访问已隔离的损坏 Session 时明确报错，而不是当作不存在。"""
        error = self._store.corrupt.get(session_id)
        if error is not None:
            raise error

    def _require_state(self, session_id: str) -> AgentStateFile:
        state = self._states.get(session_id)
        if state is None:
            self._ensure_not_corrupt(session_id)
            raise ValueError(f"session 不存在: {session_id}")
        return state

    @staticmethod
    def to_session_model(state: AgentStateFile) -> SessionModel:
        session = state.session
        return SessionModel(
            id=session.id,
            channel_id=session.channel_id,
            title=session.title,
            workspace=session.workspace,
            metadata=dict(state.metadata),
            created_at=_iso_to_epoch(session.created_at),
            updated_at=_iso_to_epoch(session.updated_at),
            last_user_text=_last_user_text(state),
        )

    @staticmethod
    def to_message_model(msg: Msg, session_id: str) -> MessageModel:
        payload = msg.model_dump(mode="json")
        return MessageModel(
            id=msg.id,
            session_id=session_id,
            name=msg.name,
            role=msg.role,
            content=payload["content"],
            metadata=payload["metadata"],
            created_at=msg.created_at,
            token=payload.get("token"),
            finished_at=msg.finished_at,
            finished_reason=payload.get("finished_reason"),
            structured_output=payload.get("structured_output"),
            error=payload.get("error"),
            timestamp=_iso_to_epoch(msg.created_at),
        )

    # ============================================================
    # Session CRUD
    # ============================================================

    async def create_session(
        self, channel_id: str, title: str = "", workspace: str = ""
    ) -> str:
        """创建新 session，返回 session_id（格式: '<channel_id>_sess_<hex12>'）"""
        sid = self.make_session_id(channel_id)
        now = _now_iso()
        state = AgentStateFile(
            session=SessionState(
                id=sid,
                channel_id=channel_id,
                title=title,
                workspace=workspace,
                created_at=now,
                updated_at=now,
            )
        )
        async with self._store.global_lock:
            await self.commit(state)
        return sid

    def make_session_id(self, channel_id: str) -> str:
        """生成 '<channel_id>_sess_<hex12>' 格式的 session_id（格式规则唯一出处）。"""
        _validate_channel_id(channel_id)
        return f"{channel_id}_{self.create_id()}"

    async def create_session_with_state(self, state: AgentStateFile) -> str:
        """用调用方已构建完整的 state 原子创建一个 session：单次 commit 落盘。

        业务规则（复制哪些消息/metadata、重生成 Msg.id 等）由 Service 层在构建
        state 时完成；本方法只做格式校验、防覆盖与跨 session Msg.id 唯一性检查，
        并在 global_lock 内一次性提交（1 次序列化 + 1 次 fsync + 1 次 replace）。

        Raises:
            ValueError: session_id 非法、已存在/损坏，或任一 Msg.id 已被占用。
        """
        validate_session_id(state.session.id)
        _validate_channel_id(state.session.channel_id)
        async with self._store.global_lock:
            if (
                state.session.id in self._states
                or state.session.id in self._store.corrupt
            ):
                raise ValueError(
                    f"session 已存在或损坏，拒绝覆盖: {state.session.id}"
                )
            # 与 save_message 的跨 session Msg.id 唯一性检查等价（绕开逐条
            # save_message 后此防线必须由本方法补齐，否则会静默劫持索引）。
            for msg in state.messages:
                owner = self._message_sessions.get(msg.id)
                if owner is not None:
                    raise ValueError(f"message 已存在: {msg.id} (session={owner})")
            await self.commit(state)
        return state.session.id

    async def get_or_create_external_session(
        self,
        channel_id: str,
        external_key: str,
        title: str = "",
        workspace: str = "",
        external_data: dict[str, Any] | None = None,
    ) -> str:
        """Get or create a local session bound to an external platform conversation."""
        _validate_channel_id(channel_id)
        if not external_key:
            raise ValueError("external_key cannot be empty")

        async with self._store.global_lock:
            session_id = self._external_sessions.get((channel_id, external_key))
            state = self._states.get(session_id) if session_id else None
            now = _now_iso()
            if state is not None:
                # 已存在：更新 external data 和 updated_at
                new_state = state.model_copy(deep=True)
                external = new_state.metadata["external"]
                external["data"] = dict(external_data or {})
                external["updated_at"] = now
                new_state.session.updated_at = now
                await self.commit(new_state)
                return session_id

            session_id = self.make_session_id(channel_id)
            state = AgentStateFile(
                session=SessionState(
                    id=session_id,
                    channel_id=channel_id,
                    title=title,
                    workspace=workspace,
                    created_at=now,
                    updated_at=now,
                ),
                metadata={
                    "external": {
                        "channel_id": channel_id,
                        "external_key": external_key,
                        "data": dict(external_data or {}),
                        "created_at": now,
                        "updated_at": now,
                    }
                },
            )
            await self.commit(state)
            return session_id

    async def get_external_session(self, session_id: str) -> ExternalSessionModel | None:
        """Look up external platform conversation metadata by local session id."""
        state = self._states.get(session_id)
        if state is None:
            self._ensure_not_corrupt(session_id)
            return None
        external = self._external_of(state)
        if external is None:
            return None
        return ExternalSessionModel(
            channel_id=external["channel_id"],
            external_key=external["external_key"],
            session_id=session_id,
            external_data=dict(external.get("data") or {}),
            created_at=_iso_to_epoch(external.get("created_at")),
            updated_at=_iso_to_epoch(external.get("updated_at")),
        )

    async def get_session(self, session_id: str) -> SessionModel | None:
        """获取 session，不存在返回 None"""
        state = self._states.get(session_id)
        if state is None:
            self._ensure_not_corrupt(session_id)
            return None
        return self.to_session_model(state)

    async def update_session(
        self,
        session_id: str,
        title: str | None = None,
        workspace: str | None = None,
    ) -> None:
        """
        更新 session（title / workspace / updated_at）。
        title 或 workspace 任一非 None 即更新对应字段；都为 None 时仅刷 updated_at。
        """
        async with self._store.lock_for(session_id):
            state = self._states.get(session_id)
            if state is None:
                self._ensure_not_corrupt(session_id)
                return
            new_state = state.model_copy(deep=True)
            if title is not None:
                new_state.session.title = title
            if workspace is not None:
                new_state.session.workspace = workspace
            new_state.session.updated_at = _now_iso()
            await self.commit(new_state)

    async def get_session_metadata(self, session_id: str) -> dict[str, Any]:
        """读取 session 的完整 metadata（解析后的 dict）。session 不存在返回空 dict。"""
        state = self._states.get(session_id)
        if state is None:
            self._ensure_not_corrupt(session_id)
            return {}
        return dict(state.metadata)

    async def update_session_metadata(
        self, session_id: str, key: str, value: Any | None
    ) -> dict[str, Any]:
        """合并写入 metadata 的单个 key。

        Args:
            key: metadata 中的字段名
            value: 要写入的值；传 None 表示删除该 key

        Returns:
            写入后的完整 metadata dict
        """
        async with self._store.lock_for(session_id):
            state = self._require_state(session_id)
            new_state = state.model_copy(deep=True)
            if value is None:
                new_state.metadata.pop(key, None)
            else:
                new_state.metadata[key] = value
            new_state.session.updated_at = _now_iso()
            await self.commit(new_state)
            return dict(new_state.metadata)

    async def mutate_session_metadata(
        self, session_id: str, key: str, updater: Callable[[Any], Any]
    ) -> dict[str, Any]:
        """原子读-改-写 metadata 的单个 key：updater(旧值) -> 新值。

        全程在 session 锁内执行，并发调用互斥，不会丢失更新。
        updater 必须是同步纯函数（无 I/O、不 await）；入参为 key 当前值
        （可能 None），返回 None 表示删除该 key。updater 抛异常时不提交，
        状态保持不变，异常向调用方传播。

        Returns:
            写入后的完整 metadata dict
        """
        async with self._store.lock_for(session_id):
            state = self._require_state(session_id)
            new_state = state.model_copy(deep=True)
            new_value = updater(new_state.metadata.get(key))
            if new_value is None:
                new_state.metadata.pop(key, None)
            else:
                new_state.metadata[key] = new_value
            new_state.session.updated_at = _now_iso()
            await self.commit(new_state)
            return dict(new_state.metadata)

    async def delete_session(self, session_id: str) -> None:
        """删除 session 及其所有 messages（只删除精确目标文件）"""
        async with self._store.global_lock, self._store.lock_for(session_id):
            state = self._states.pop(session_id, None)
            if state is not None:
                for message in state.messages:
                    self._message_sessions.pop(message.id, None)
                stale = [
                    k for k, v in self._external_sessions.items() if v == session_id
                ]
                for key in stale:
                    self._external_sessions.pop(key, None)
            await self._store.delete(session_id)
            self._store.locks.pop(session_id, None)

    async def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
        channel_id: str | None = None,
        workspace: str | None = None,
    ) -> list[SessionModel]:
        """
        列出 sessions（按 updated_at 倒序）。

        Args:
            limit:      返回数量上限
            offset:     偏移量（分页用）
            channel_id: 非空时仅返回该 channel
            workspace:  非 None 时仅返回该 workspace（空串 "" = 未设置工作区的会话）
        """
        states = self._filter_states(channel_id=channel_id, workspace=workspace)
        states.sort(key=lambda s: _iso_to_epoch(s.session.updated_at), reverse=True)
        return [
            self.to_session_model(state) for state in states[offset:offset + limit]
        ]

    async def count_sessions(
        self,
        channel_id: str | None = None,
        workspace: str | None = None,
    ) -> int:
        """返回 sessions 总数（用于分页 total）"""
        return len(self._filter_states(channel_id=channel_id, workspace=workspace))

    def _filter_states(
        self,
        *,
        channel_id: str | None = None,
        workspace: str | None = None,
    ) -> list[AgentStateFile]:
        states = []
        for state in self._states.values():
            if channel_id and state.session.channel_id != channel_id:
                continue
            if workspace is not None and state.session.workspace != workspace:
                continue
            states.append(state)
        return states

    async def list_workspaces(self, channel_id: str | None = None) -> list[dict]:
        """
        枚举所有出现过的 workspace，按各自最新活跃时间倒序。

        每个 workspace 返回：
        - workspace: 工作区路径（"" = 未设置）
        - session_count: 该工作区下的会话数
        - latest_at: 该工作区下最新会话的 updated_at

        Args:
            channel_id: 非空时仅统计该 channel（如 "ws"）下的工作区
        """
        grouped: dict[str, dict] = {}
        for state in self._filter_states(channel_id=channel_id):
            workspace = state.session.workspace or ""
            updated = _iso_to_epoch(state.session.updated_at)
            entry = grouped.setdefault(
                workspace, {"workspace": workspace, "session_count": 0, "latest_at": 0.0}
            )
            entry["session_count"] += 1
            entry["latest_at"] = max(entry["latest_at"], updated)
        return sorted(grouped.values(), key=lambda e: e["latest_at"], reverse=True)

    # ============================================================
    # Mailbox（SessionLane 的持久请求队列）
    # ============================================================

    @staticmethod
    def _find_pending_request(
        state: AgentStateFile, request_id: str
    ) -> QueueItem | None:
        """只在持久化 pending 中查找尚未领取的重复请求。"""
        if not request_id:
            return None
        return next(
            (
                item
                for item in state.mailbox.pending
                if item.request_id == request_id
            ),
            None,
        )

    @staticmethod
    def _has_persisted_user_message(
        state: AgentStateFile, request_id: str
    ) -> bool:
        """messages 是领取后请求的持久化幂等凭据，不另存完成结果。"""
        if not request_id:
            return False
        return any(
            message.role == "user"
            and str(message.metadata.get("request_id") or "") == request_id
            for message in state.messages
        )

    async def admit_request(
        self,
        session_id: str,
        *,
        request_id: str,
        content: str,
        attachments: list[dict[str, Any]],
        agent_id: str,
        capacity: int = 100,
    ) -> tuple[bool, int]:
        """原子接纳一条消息；返回 ``(created, queue_position)``。

        ``request_id`` 在一个 session 内是幂等键。只有 state.json 原子
        落盘成功以后才返回 created=True，因此调用方可以把 accepted 当成耐久确认。
        """
        # 下面整个临界区保证幂等检查、容量检查、sequence 分配和 state.json 提交不可分割；
        # 重试同一个 request_id 只返回已有接纳结果，不会再次执行工具副作用。
        async with self._store.lock_for(session_id):
            state = self._require_state(session_id)
            existing = self._find_pending_request(state, request_id)
            if existing is not None:
                position = next(
                    index
                    for index, item in enumerate(state.mailbox.pending, start=1)
                    if item.request_id == existing.request_id
                )
                return False, position
            if self._has_persisted_user_message(state, request_id):
                # 已被领取的请求一定已将 UserMessage 写入 messages；即使 Gateway
                # 后续中断，也不应因同一 request_id 重试而重复执行。
                return False, 0

            used = len(state.mailbox.pending)
            if used >= capacity:
                raise OverflowError(f"mailbox 已满: {used}/{capacity}")

            now = _now_iso()
            new_state = state.model_copy(deep=True)
            item = QueueItem(
                request_id=request_id,
                sequence=new_state.mailbox.next_sequence,
                content=content,
                attachments=copy.deepcopy(attachments),
                agent_id=agent_id or "default",
            )
            new_state.mailbox.next_sequence += 1
            new_state.mailbox.pending.append(item)
            new_state.mailbox.revision += 1
            new_state.session.updated_at = now
            await self.commit(new_state)
            return True, len(new_state.mailbox.pending)

    async def peek_request(self, session_id: str) -> QueueItem | None:
        async with self._store.lock_for(session_id):
            state = self._states.get(session_id)
            if state is None or not state.mailbox.pending:
                return None
            return state.mailbox.pending[0].model_copy(deep=True)

    async def take_pending_request(
        self, session_id: str, request_id: str
    ) -> QueueItem | None:
        """原子移除队首，交给 SessionLane 的内存执行态。

        这是刻意选择的 at-most-once 交接点：提交后崩溃不会自动重放该请求。
        TurnExecutor 随后会将 UserMessage 写入 messages；若恰在两者之间崩溃，
        用户消息允许丢失，这是本项目明确接受的少数异常语义。
        """
        async with self._store.lock_for(session_id):
            state = self._require_state(session_id)
            if (
                not state.mailbox.pending
                or state.mailbox.pending[0].request_id != request_id
            ):
                return None
            new_state = state.model_copy(deep=True)
            now = _now_iso()
            item = new_state.mailbox.pending.pop(0)
            new_state.mailbox.revision += 1
            new_state.session.updated_at = now
            await self.commit(new_state)
            return item.model_copy(deep=True)

    async def cancel_pending_request(
        self,
        session_id: str,
        request_id: str,
    ) -> QueueItem | None:
        """原子移除一条仍在 pending 的消息。

        已被 SessionLane 领取的请求不在磁盘队列中，必须走 cancel_current。
        取消后不写完成结果：横幅只依赖 pending，移除后下个快照自然消失。
        """
        async with self._store.lock_for(session_id):
            state = self._require_state(session_id)
            index = next(
                (
                    index
                    for index, item in enumerate(state.mailbox.pending)
                    if item.request_id == request_id
                ),
                -1,
            )
            if index < 0:
                return None

            now = _now_iso()
            new_state = state.model_copy(deep=True)
            item = new_state.mailbox.pending.pop(index)
            new_state.mailbox.revision += 1
            new_state.session.updated_at = now
            await self.commit(new_state)
            return item.model_copy(deep=True)

    async def mailbox_snapshot(self, session_id: str) -> MailboxState:
        async with self._store.lock_for(session_id):
            state = self._states.get(session_id)
            if state is None:
                return MailboxState()
            return state.mailbox.model_copy(deep=True)

    async def advance_mailbox_revision(self, session_id: str) -> int:
        """推进 mailbox 快照版本，但不记录任何运行态对象。

        ``pending`` 未变化时，SessionLane 仍可能发生 ``running → idle``、
        ``running → compacting`` 等对客户端可见的状态变化。revision 是这些
        快照的单调版本号；它只保存一个整数，绝不把 active 或完成结果写回磁盘。
        """
        async with self._store.lock_for(session_id):
            state = self._require_state(session_id)
            new_state = state.model_copy(deep=True)
            new_state.mailbox.revision += 1
            new_state.session.updated_at = _now_iso()
            await self.commit(new_state)
            return new_state.mailbox.revision

    async def mailbox_session_ids(self) -> list[str]:
        return [
            session_id
            for session_id, state in self._states.items()
            if state.mailbox.pending
        ]

    # ============================================================
    # Message（Msg 快照）
    # ============================================================

    async def save_message(
        self,
        session_id: str,
        message: Msg | dict[str, Any],
        *,
        timestamp: float | None = None,
    ) -> str:
        """保存一条完整 Msg；流式 Event 不属于这个存储边界。

        timestamp 参数仅为旧接口兼容保留，磁盘不再保存单独的
        timestamp；排序以 messages 数组顺序为准，对外游标由 created_at 派生。
        """
        del timestamp  # 见 docstring
        msg = message if isinstance(message, Msg) else Msg.model_validate(message)
        async with self._store.lock_for(session_id):
            state = self._require_state(session_id)
            owner = self._message_sessions.get(msg.id)
            if owner is not None:
                raise ValueError(f"message 已存在: {msg.id} (session={owner})")
            new_state = state.model_copy(deep=True)
            # 深拷贝隔离：调用方持有的 Msg 不能直接改到内部缓存
            new_state.messages.append(msg.model_copy(deep=True))
            new_state.session.updated_at = _now_iso()
            await self.commit(new_state)
        return msg.id

    async def update_message(self, message: Msg | dict[str, Any]) -> None:
        """更新已持久化 Msg 的可变快照字段，不改变数组中的位置。"""
        await self.update_messages([message])

    async def update_messages(
        self, messages: list[Msg | dict[str, Any]]
    ) -> None:
        """批量更新同一 Session 的 Msg，一次性原子提交完整 state。

        调用方可以按任意顺序传入消息；磁盘 transcript 始终保持原数组顺序。
        所有 id 与所属 session 会在写盘前完成校验，任一消息不存在、重复或
        跨 session 时整体失败，不产生部分更新。
        """
        msgs = [
            message if isinstance(message, Msg) else Msg.model_validate(message)
            for message in messages
        ]
        if not msgs:
            return

        message_ids = [message.id for message in msgs]
        if len(set(message_ids)) != len(message_ids):
            raise ValueError("批量更新含重复 message id")

        owners: set[str] = set()
        for message_id in message_ids:
            owner = self._message_sessions.get(message_id)
            if owner is None:
                raise ValueError(f"message 不存在: {message_id}")
            owners.add(owner)
        if len(owners) != 1:
            raise ValueError("批量更新的 message 跨 session")
        session_id = next(iter(owners))

        async with self._store.lock_for(session_id):
            state = self._require_state(session_id)
            indexes = {
                existing.id: index
                for index, existing in enumerate(state.messages)
            }
            missing = [
                message_id
                for message_id in message_ids
                if message_id not in indexes
            ]
            if missing:  # pragma: no cover - 索引与状态不一致的兜底
                raise ValueError(f"message 不存在: {missing[0]}")

            new_state = state.model_copy(deep=True)
            for message in msgs:
                new_state.messages[indexes[message.id]] = message.model_copy(deep=True)
            new_state.session.updated_at = _now_iso()
            await self.commit(new_state)

    async def get_messages_by_session(self, session_id: str) -> list[MessageModel]:
        """获取指定 session 的完整 transcript（按消息顺序正序）。

        供 HTTP API / Desktop 历史展示使用；给 LLM 构建上下文请用
        ContextService.get_context_messages()。
        """
        state = self._states.get(session_id)
        if state is None:
            self._ensure_not_corrupt(session_id)
            return []
        return [self.to_message_model(m, session_id) for m in state.messages]

    async def upsert_message(
        self, session_id: str, message: Msg | dict[str, Any]
    ) -> str:
        """按 id 幂等写入：存在则更新，不存在则追加。

        供 SessionProjection 投影 context_compact_done 时使用——同一 Event id
        重放不会产生重复 Msg。
        """
        msg = message if isinstance(message, Msg) else Msg.model_validate(message)
        async with self._store.lock_for(session_id):
            state = self._require_state(session_id)
            new_state = state.model_copy(deep=True)
            for index, existing in enumerate(new_state.messages):
                if existing.id == msg.id:
                    new_state.messages[index] = msg.model_copy(deep=True)
                    new_state.session.updated_at = _now_iso()
                    await self.commit(new_state)
                    return msg.id
            new_state.messages.append(msg.model_copy(deep=True))
            new_state.session.updated_at = _now_iso()
            await self.commit(new_state)
        return msg.id
