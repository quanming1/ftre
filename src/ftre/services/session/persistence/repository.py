"""SessionRepository —— Session 纯数据存取（CRUD + 索引 + 提交）。

只负责把 SessionMetaFile 搬进搬出并维护索引，不含任何业务规则
（上下文裁剪 / token 计算 / 前端投影等归 Service 层）。

并发模型：per-session asyncio.Lock + 全局 create/delete 锁；
写盘采用临时文件 + fsync + os.replace 原子替换，写盘成功后才提交内存缓存。

会话数据只从 ``sessions/`` 目录中的当前 JSON 模型读取，不提供旧格式迁移。

Repository 是 SessionService 的存储实现，不是可被 Feature 直接注入的 Service；
只有 SessionService 能决定什么时候写入消息、什么时候发出 lifecycle/flush Hook。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ftre_agent.message import Msg, MsgName

from ftre.services.config.paths import CONFIG_PATH
from ftre.services.session.entity.models import (
    ExternalSessionModel,
    MessageModel,
    SessionModel,
)
from ftre.services.session.entity.state import (
    SessionMetaFile,
    SessionState,
)

from .json_store import JsonStateStore, validate_session_id

logger = logging.getLogger(__name__)

# 该参数保留为构造函数的目录锚点；实际持久化始终使用 sessions/ JSON 文件。
DEFAULT_DB_PATH = str(CONFIG_PATH.parent / "sessions.db")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


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


def summarize_last_user_text(messages: list[Msg]) -> str:
    """从派生消息中提取最后一条真实用户消息摘要（供反规范化 last_user_text 字段）。

    "真实用户消息"判定：role == user 且 name == default（跳过 compact 摘要）。
    """
    for msg in reversed(messages):
        if msg.role != "user":
            continue
        if msg.name != MsgName.DEFAULT.value:
            continue
        if msg.metadata.get("hide"):
            continue
        texts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        joined = " ".join(t.strip() for t in texts if t and t.strip())
        joined = re.sub(r"\s+", " ", joined).strip()
        if joined:
            return joined[:_LAST_USER_TEXT_MAX]
    return ""


class SessionRepository:
    """Session 数据存取唯一入口；调用方不应直接读写 session.json/session.jsonl。"""

    def __init__(self, db_path: str | None = None, *, sessions_dir: str | None = None):
        # db_path 仅用于推导 sessions/ 所在的配置目录。
        self._db_path = db_path or DEFAULT_DB_PATH
        root = Path(sessions_dir) if sessions_dir else Path(self._db_path).parent / "sessions"
        self._sessions_root = root
        self._store = JsonStateStore(root)
        self._states = self._store.states  # 引用同一 dict（store 负责清空/填充）
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

    def get_state(self, session_id: str) -> SessionMetaFile | None:
        """读取内存中的会话元信息；不存在返回 None（损坏 session 明确报错）。"""
        state = self._states.get(session_id)
        if state is None:
            self._ensure_not_corrupt(session_id)
            return None
        return state

    def all_states(self) -> list[tuple[str, SessionMetaFile]]:
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

    async def commit(self, new_state: SessionMetaFile) -> None:
        """原子写盘成功后提交内存缓存（并发场景调用方必须已持有对应锁）。"""
        await self._store.write(new_state)
        session_id = new_state.session.id
        self._states[session_id] = new_state
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
        self._external_sessions.clear()
        for session_id, state in self._states.items():
            external = self._external_of(state)
            if external is not None:
                key = (external["channel_id"], external["external_key"])
                self._external_sessions[key] = session_id

    @staticmethod
    def _external_of(state: SessionMetaFile) -> dict[str, Any] | None:
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

    def _require_state(self, session_id: str) -> SessionMetaFile:
        state = self._states.get(session_id)
        if state is None:
            self._ensure_not_corrupt(session_id)
            raise ValueError(f"session 不存在: {session_id}")
        return state

    @staticmethod
    def to_session_model(state: SessionMetaFile) -> SessionModel:
        session = state.session
        return SessionModel(
            id=session.id,
            agent_id=session.agent_id,
            channel_id=session.channel_id,
            title=session.title,
            workspace=session.workspace,
            metadata=dict(state.metadata),
            created_at=_iso_to_epoch(session.created_at),
            updated_at=_iso_to_epoch(session.updated_at),
            last_user_text=session.last_user_text,
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
        state = SessionMetaFile(
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

    async def create_session_with_state(self, state: SessionMetaFile) -> str:
        """用调用方已构建完整的元信息原子创建一个 session：单次 commit 落盘。

        Raises:
            ValueError: session_id 非法、已存在或损坏。
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
            state = SessionMetaFile(
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
    ) -> list[SessionMetaFile]:
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

    # ============================================================
    # 消息事实由 SessionLog + session.jsonl 承载（PRD-F43）。
    # 本 Repository 不保存消息；派生读取统一走 SessionService。
    # ============================================================

    def session_dir(self, session_id: str) -> Path:
        """事件日志所在的会话目录（session.jsonl 与 session.json 同目录）。"""
        return self._store.session_dir(session_id)

    async def set_last_user_text(self, session_id: str, text: str) -> None:
        """维护反规范化预览字段（SessionService 在 user/message 事件后调用）。"""
        async with self._store.lock_for(session_id):
            state = self._states.get(session_id)
            if state is None:
                self._ensure_not_corrupt(session_id)
                return
            new_state = state.model_copy(deep=True)
            new_state.session.last_user_text = text
            await self.commit(new_state)
