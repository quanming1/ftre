"""SessionManager - 会话与消息持久化（Agent State JSON Store）

每个 Session 一份 ``~/.ftre/sessions/<base64url(session_id)>/state.json``：

- session:  会话元信息（id, agent_id, channel_id, title, workspace, created_at, updated_at）
- messages: 完整 Msg 快照，数组顺序即消息顺序；流式 AgentStreamEvent 不持久化
- summary:  当前滚动摘要（SystemMsg + through_message_id 游标），不属于 messages
- metadata: 会话级扩展数据（plan / external 等）

并发模型：per-session asyncio.Lock + 全局 create/delete 锁；
写盘采用临时文件 + fsync + os.replace 原子替换，写盘成功后才提交内存缓存。

旧 ``sessions.db`` 不做迁移兼容：启动时直接删除遗留文件（含 wal/shm），
历史数据不保留。
"""
from __future__ import annotations

import asyncio
import time
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from ftre_agent_core.message import Msg

from ftre.config import CONFIG_PATH
from ftre.session.json_store import CorruptStateError, JsonStateStore
from ftre.session.state import AgentStateFile, SessionState, SummaryState


class SessionModel(TypedDict):
    """会话元信息"""
    id: str              # 会话唯一标识（格式: '<channel_id>_sess_<hex12>'）
    channel_id: str      # 来源 channel（如 'ws' / 'cron' / 'cli'）
    title: str           # 对话标题
    workspace: str       # 当前工作区绝对路径（cwd 来源；为空表示未设置）
    metadata: dict       # 会话级元数据（JSON 解析后的 dict，如 plan 等）
    created_at: float    # 创建时间戳
    updated_at: float    # 最后活跃时间戳


class MessageModel(TypedDict):
    """持久化的 Msg 快照。"""
    id: str              # Msg.id
    session_id: str      # 所属会话 ID
    name: str
    role: str
    content: list[dict[str, Any]]
    metadata: dict[str, Any]
    created_at: str
    usage: dict[str, int] | None
    finished_at: str | None
    finished_reason: str | None
    structured_output: dict[str, Any] | None
    error: dict[str, Any] | None
    timestamp: float     # 排序/分页游标（由 created_at 派生）


class ExternalSessionModel(TypedDict):
    channel_id: str
    external_key: str
    session_id: str
    external_data: dict[str, Any]
    created_at: float
    updated_at: float


logger = logging.getLogger(__name__)


# 旧 SQLite 数据库路径：~/.ftre/sessions.db（不再使用，启动时直接删除）
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
import re as _re

_CHANNEL_ID_RE = _re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_channel_id(channel_id: str) -> None:
    if not channel_id:
        raise ValueError("channel_id 不能为空")
    if not _CHANNEL_ID_RE.match(channel_id):
        raise ValueError(
            f"channel_id 含非法字符（只允许 [A-Za-z0-9_-]）: {channel_id!r}"
        )


class SessionManager:
    """Session 持久化唯一入口；调用方不应直接读写 state.json。"""

    def __init__(self, db_path: str | None = None, *, sessions_dir: str | None = None):
        # db_path 仅用于推导配置目录（sessions/ 与其同级）；旧库文件会被删除
        self._db_path = db_path or DEFAULT_DB_PATH
        root = Path(sessions_dir) if sessions_dir else Path(self._db_path).parent / "sessions"
        self._store = JsonStateStore(root)
        self._states = self._store.states  # 引用同一 dict（store 负责清空/填充）
        # Msg.id → session_id（Msg.id 在配置目录内全局唯一）
        self._message_sessions: dict[str, str] = {}
        # (channel_id, external_key) → session_id
        self._external_sessions: dict[tuple[str, str], str] = {}

    async def init(self) -> None:
        """启动：删除遗留 sessions.db（不迁移），加载全部 JSON 状态并建索引。"""
        await self._discard_legacy_db()
        await self._store.load_all()
        self._rebuild_indexes()

    async def _discard_legacy_db(self) -> None:
        """直接删除遗留 SQLite 文件（含 wal/shm/journal），不做数据迁移。"""
        legacy = Path(self._db_path)
        if not legacy.exists():
            return
        logger.warning(
            "[session-store] 删除遗留 sessions.db（不迁移兼容）: %s", legacy
        )
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(legacy) + suffix)
            try:
                if path.exists():
                    await asyncio.to_thread(path.unlink)
            except OSError:
                logger.exception("[session-store] 删除失败: %s", path)

    async def close(self) -> None:
        """安全幂等：JSON Store 无长连接，仅清理内存状态。"""
        # 幂等 no-op：保留磁盘状态，重复调用安全
        return None

    def create_id(self) -> str:
        """生成新的 session_id"""
        return f"sess_{uuid.uuid4().hex[:12]}"

    # ============================================================
    # 内部：索引 / 边界转换 / 状态提交
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

    async def _commit(self, new_state: AgentStateFile) -> None:
        """原子写盘成功后提交内存缓存（调用方必须已持有对应锁）。"""
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

    @staticmethod
    def _to_session_model(state: AgentStateFile) -> SessionModel:
        session = state.session
        return SessionModel(
            id=session.id,
            channel_id=session.channel_id,
            title=session.title,
            workspace=session.workspace,
            metadata=dict(state.metadata),
            created_at=_iso_to_epoch(session.created_at),
            updated_at=_iso_to_epoch(session.updated_at),
        )

    @staticmethod
    def _to_message_model(msg: Msg, session_id: str) -> MessageModel:
        payload = msg.model_dump(mode="json")
        return MessageModel(
            id=msg.id,
            session_id=session_id,
            name=msg.name,
            role=msg.role,
            content=payload["content"],
            metadata=payload["metadata"],
            created_at=msg.created_at,
            usage=payload.get("usage"),
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
        _validate_channel_id(channel_id)
        sid = f"{channel_id}_{self.create_id()}"
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
            await self._commit(state)
        return sid

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
                await self._commit(new_state)
                return session_id

            session_id = f"{channel_id}_{self.create_id()}"
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
            await self._commit(state)
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
        return self._to_session_model(state)

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
            await self._commit(new_state)

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
            await self._commit(new_state)
            return dict(new_state.metadata)

    async def delete_session(self, session_id: str) -> None:
        """删除 session 及其所有 messages（只删除精确目标文件）"""
        async with self._store.global_lock:
            async with self._store.lock_for(session_id):
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
            self._to_session_model(state) for state in states[offset:offset + limit]
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
            await self._commit(new_state)
        return msg.id

    async def update_message(self, message: Msg | dict[str, Any]) -> None:
        """更新已持久化 Msg 的可变快照字段，不改变数组中的位置。"""
        msg = message if isinstance(message, Msg) else Msg.model_validate(message)
        session_id = self._message_sessions.get(msg.id)
        if session_id is None:
            raise ValueError(f"message 不存在: {msg.id}")
        async with self._store.lock_for(session_id):
            state = self._require_state(session_id)
            new_state = state.model_copy(deep=True)
            for index, existing in enumerate(new_state.messages):
                if existing.id == msg.id:
                    new_state.messages[index] = msg.model_copy(deep=True)
                    break
            else:  # pragma: no cover - 索引与状态不一致的兜底
                raise ValueError(f"message 不存在: {msg.id}")
            new_state.session.updated_at = _now_iso()
            await self._commit(new_state)

    async def get_messages_by_session(self, session_id: str) -> list[MessageModel]:
        """获取指定 session 的完整 transcript（按消息顺序正序）。

        供 HTTP API / Desktop 历史展示使用；给 LLM 构建上下文请用
        get_context_messages()。
        """
        state = self._states.get(session_id)
        if state is None:
            self._ensure_not_corrupt(session_id)
            return []
        return [self._to_message_model(m, session_id) for m in state.messages]

    async def get_context_messages(self, session_id: str) -> list[MessageModel]:
        """返回给 LLM 使用的 summary + tail（不含被摘要覆盖的历史）。

        无摘要时等同 get_messages_by_session()；有摘要时返回
        [summary.message, *through_message_id 之后的 Msg]。
        摘要 SystemMsg 保留 metadata.context_compact.mode == "summary"，
        现有 converter 可继续识别。
        """
        state = self._states.get(session_id)
        if state is None:
            self._ensure_not_corrupt(session_id)
            return []
        if state.summary is None:
            return [self._to_message_model(m, session_id) for m in state.messages]

        cursor = self._summary_cursor_index(state)
        records = [self._to_message_model(state.summary.message, session_id)]
        records.extend(
            self._to_message_model(m, session_id)
            for m in state.messages[cursor + 1:]
        )
        return records

    async def get_summary(self, session_id: str) -> SummaryState | None:
        """返回当前滚动摘要（深拷贝，调用方修改不影响缓存）。"""
        state = self._states.get(session_id)
        if state is None:
            self._ensure_not_corrupt(session_id)
            return None
        if state.summary is None:
            return None
        return state.summary.model_copy(deep=True)

    async def save_summary(
        self,
        session_id: str,
        message: Msg,
        *,
        through_message_id: str,
    ) -> None:
        """原子更新当前摘要，不把摘要加入 transcript。

        compact 在锁外完成 LLM 摘要后调用本方法：持 Session 锁、
        基于当前缓存中的最新 state 只更新 summary 字段，因此 compact
        期间新增的消息会自然保留在摘要游标之后，不会被旧副本覆盖。
        """
        if message.role != "system":
            raise ValueError("summary.message must be a SystemMsg")
        async with self._store.lock_for(session_id):
            state = self._require_state(session_id)
            if through_message_id not in {m.id for m in state.messages}:
                raise ValueError(
                    f"summary cursor 不存在于 messages: {through_message_id}"
                )
            new_state = state.model_copy(deep=True)
            new_state.summary = SummaryState(
                message=message.model_copy(deep=True),
                through_message_id=through_message_id,
            )
            new_state.session.updated_at = _now_iso()
            await self._commit(new_state)

    @staticmethod
    def _summary_cursor_index(state: AgentStateFile) -> int:
        """through_message_id 在 messages 中的下标；不存在时按覆盖全部处理。"""
        assert state.summary is not None
        for index, message in enumerate(state.messages):
            if message.id == state.summary.through_message_id:
                return index
        return len(state.messages) - 1

    async def get_recent_messages_by_turns(
        self, session_id: str, limit_turns: int = 5, before_ts: float | None = None
    ) -> tuple[list[MessageModel], bool]:
        """获取指定 session 最近 N 轮对话的所有消息。

        一轮 = 一条可见 user Msg，到下一条可见 user Msg（或末尾）之间的消息。

        Args:
            limit_turns: 返回最近 N 轮
            before_ts: 可选游标，只考虑 timestamp < before_ts 的消息（用于加载更早）

        Returns:
            (messages, has_more): messages 按时间正序；has_more 表示是否还有更早的消息。
        """
        records = await self.get_messages_by_session(session_id)
        if before_ts is not None:
            records = [r for r in records if r["timestamp"] < before_ts]
        if not records:
            return [], False

        # 从后向前找最近 limit_turns 条可见 UserMsg
        visible_user_indexes = [
            index
            for index, record in enumerate(records)
            if record["role"] == "user" and not record["metadata"].get("hide", False)
        ]
        if not visible_user_indexes:
            return [], False

        target = visible_user_indexes[-limit_turns:]
        start = target[0]
        messages = records[start:]
        has_more = start > 0
        return messages, has_more

    # ============================================================
    # Token 用量（最近一次 LLM 实算 + 之后未计入消息的字符级粗估）
    # ============================================================

    async def get_token_usage(self, session_id: str) -> dict:
        """
        计算指定 session 当前 token 用量。

        对 get_context_messages()（summary + tail）计算，而不是完整
        transcript——否则 compact 后原始消息仍在 messages，统计会一直
        认为上下文没有缩小。

        策略：
        - 找上下文中最晚的"携带 usage 的 assistant Msg"作为 anchor
        - anchor 之后还没被 LLM 计入但会进下次 prompt 的 Msg 用字符级粗估
        - total = anchor.total_tokens + pending_estimated
        - 没有 anchor 时（全新 session）退化为对全量上下文估算
        """
        messages = await self.get_context_messages(session_id)
        return _compute_token_usage(session_id, messages)

    # ============================================================
    # 历史恢复
    # ============================================================

    # Msg → provider 消息的转换统一位于 converter.py。


def _find_anchor(messages: list[MessageModel]) -> tuple[int, dict | None, str]:
    """倒序找最晚的带 usage 的 Msg。"""
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        usage_data = message.get("usage") or {}
        input_tokens = usage_data.get("input_tokens", 0)
        output_tokens = usage_data.get("output_tokens", 0)
        if input_tokens or output_tokens:
            usage = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
            return i, usage, "msg"
    return -1, None, ""


def _build_anchor_payload(usage: dict, timestamp: float, source: str) -> dict:
    """把 LLM 上报的 usage dict 整理成对外 payload，补全 total_tokens"""
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "at": timestamp,
        "source": source,
    }


def _compute_token_usage(session_id: str, messages: list[MessageModel]) -> dict:
    """
    根据 Msg 快照计算 token 用量。抽出来便于单测，不依赖存储。

    见 SessionManager.get_token_usage 文档。
    """
    from .token_counter import estimate_messages_tokens

    anchor_index, anchor_usage, anchor_source = _find_anchor(messages)

    # 锚点之后的消息用字符级粗估（无锚点时即全量估算）
    pending_messages = messages[anchor_index + 1:] if anchor_index >= 0 else messages
    pending_estimated = estimate_messages_tokens(pending_messages)

    if anchor_usage is None:
        return {
            "session_id": session_id,
            "anchor": None,
            "pending_estimated": pending_estimated,
            "total": pending_estimated,
        }

    anchor = _build_anchor_payload(
        anchor_usage, messages[anchor_index]["timestamp"], anchor_source
    )
    return {
        "session_id": session_id,
        "anchor": anchor,
        "pending_estimated": pending_estimated,
        "total": anchor["total_tokens"] + pending_estimated,
    }
