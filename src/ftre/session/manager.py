"""
SessionManager - 会话与消息持久化（SQLite）

两张表：
- sessions: 会话元信息（id, channel_id, title, created_at, updated_at）
- messages: 事件流（id, session_id, type, data, timestamp）
"""
import json
import time
import uuid
import logging
from pathlib import Path
from typing import Any, TypedDict

import aiosqlite

from ftre.config import CONFIG_PATH


class SessionModel(TypedDict):
    """会话元信息"""
    id: str              # 会话唯一标识（含 channel 前缀，如 'ws::sess_xxx'）
    channel_id: str      # 来源 channel（如 'ws' / 'cron' / 'cli'）
    title: str           # 对话标题
    created_at: float    # 创建时间戳
    updated_at: float    # 最后活跃时间戳


class MessageModel(TypedDict):
    """事件/消息记录"""
    id: str              # 消息唯一标识
    session_id: str      # 所属会话 ID
    type: str            # 事件类型（USER_INPUT / TOOL_CALL / TOOL_RESULT / MESSAGE_COMPLETE / ...）
    data: dict[str, Any] # 事件数据（JSON）
    timestamp: float     # 事件时间戳

logger = logging.getLogger(__name__)


# 默认数据库路径：~/.ftre/sessions.db，与 config.json 同目录
DEFAULT_DB_PATH = str(CONFIG_PATH.parent / "sessions.db")


class SessionManager:

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or DEFAULT_DB_PATH
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """初始化数据库连接并建表"""
        # 保证目标目录存在（首次启动 ~/.ftre 可能还没建）
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id          TEXT PRIMARY KEY,
                channel_id  TEXT NOT NULL DEFAULT '',
                title       TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id          TEXT PRIMARY KEY,
                session_id  TEXT NOT NULL,
                type        TEXT NOT NULL,
                data        TEXT NOT NULL DEFAULT '{}',
                timestamp   REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, timestamp ASC);
        """)
        # 老库迁移：sessions 表存量没有 channel_id 列时补上
        await self._migrate_add_column(
            "sessions", "channel_id", "TEXT NOT NULL DEFAULT ''"
        )
        # 索引：channel_id + updated_at（按 channel 过滤会话列表用）
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_channel "
            "ON sessions(channel_id, updated_at DESC)"
        )
        # 老数据回填：channel_id 为空时尝试从 id 前缀（'<ch>::sess_xxx'）解析
        await self._db.execute(
            "UPDATE sessions "
            "SET channel_id = substr(id, 1, instr(id, '::') - 1) "
            "WHERE channel_id = '' AND instr(id, '::') > 0"
        )
        await self._db.commit()

    async def _migrate_add_column(
        self, table: str, column: str, decl: str
    ) -> None:
        """如果 table 上没有 column，则 ALTER TABLE 加上"""
        cursor = await self._db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        existing = {r["name"] for r in rows}
        if column in existing:
            return
        await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        logger.warning(f"[session] 迁移：{table}.{column} 已添加")

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._db:
            await self._db.close()
            self._db = None

    def create_id(self) -> str:
        """生成新的 session_id"""
        return f"sess_{uuid.uuid4().hex[:12]}"

    # ============================================================
    # Session CRUD
    # ============================================================

    async def create_session(self, channel_id: str, title: str = "") -> str:
        """创建新 session，返回带 channel_id 前缀的 session_id（格式: '{channel_id}::sess_xxx'）"""
        if not channel_id:
            raise ValueError("channel_id 不能为空")
        sid = f"{channel_id}::{self.create_id()}"
        now = time.time()
        await self._db.execute(
            "INSERT INTO sessions (id, channel_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, channel_id, title, now, now),
        )
        await self._db.commit()
        return sid

    async def get_session(self, session_id: str) -> SessionModel | None:
        """获取 session，不存在返回 None"""
        cursor = await self._db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return SessionModel(
            id=row["id"],
            channel_id=row["channel_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def update_session(self, session_id: str, title: str | None = None) -> None:
        """更新 session（title 和/或 updated_at）"""
        now = time.time()
        if title is not None:
            await self._db.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, session_id),
            )
        else:
            await self._db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        await self._db.commit()

    async def delete_session(self, session_id: str) -> None:
        """删除 session 及其所有 messages"""
        await self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self._db.commit()

    async def list_sessions(
        self, limit: int = 200, channel_id: str | None = None
    ) -> list[SessionModel]:
        """
        列出最近的 sessions（按 updated_at 倒序）。
        channel_id 非空时仅返回该 channel 的会话。
        """
        if channel_id:
            cursor = await self._db.execute(
                "SELECT * FROM sessions WHERE channel_id = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (channel_id, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [
            SessionModel(
                id=r["id"],
                channel_id=r["channel_id"],
                title=r["title"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # ============================================================
    # Message（事件流）
    # ============================================================

    async def save_message(self, session_id: str, type: str, data: dict[str, Any]) -> str:
        """
        保存一条消息/事件到指定 session。
        同时更新 session 的 updated_at。
        返回生成的 message id。
        """
        msg_id = uuid.uuid4().hex[:16]
        now = time.time()
        await self._db.execute(
            "INSERT INTO messages (id, session_id, type, data, timestamp) VALUES (?, ?, ?, ?, ?)",
            (msg_id, session_id, type, json.dumps(data, ensure_ascii=False), now),
        )
        await self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        await self._db.commit()
        return msg_id

    async def get_messages_by_session(self, session_id: str) -> list[MessageModel]:
        """获取指定 session 的全部消息（按时间正序）"""
        cursor = await self._db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [
            MessageModel(
                id=r["id"],
                session_id=r["session_id"],
                type=r["type"],
                data=json.loads(r["data"]),
                timestamp=r["timestamp"],
            )
            for r in rows
        ]

    # ============================================================
    # Token 用量（最近一次 LLM 实算 + 之后未计入事件的字符级粗估）
    # ============================================================

    async def get_token_usage(self, session_id: str) -> dict:
        """
        计算指定 session 当前 token 用量。

        策略：
        - 找事件流中最晚的"携带 usage 的事件"作为 anchor
          （usage_update，或 done.data.usage 非空）
        - anchor 之后还没被 LLM 计入但会进下次 prompt 的事件用字符级粗估
        - total = anchor.total_tokens + pending_estimated
        - 没有 anchor 时（全新 session）退化为对全量回放事件估算

        Returns:
            {
              "session_id": str,
              "anchor": {
                "prompt_tokens": int,
                "completion_tokens": int,
                "total_tokens": int,
                "at": float,
                "source": "usage_update" | "done"
              } | None,
              "pending_estimated": int,
              "total": int
            }
        """
        events = await self.get_messages_by_session(session_id)
        return _compute_token_usage(session_id, events)

    # ============================================================
    # 历史恢复
    # ============================================================

    @staticmethod
    def to_openai_messages(events: list[MessageModel]) -> list[dict]:
        """
        将事件流重建为 OpenAI 格式消息列表。

        转换规则：
        - USER_INPUT        → {"role": "user", "content": ...}
        - TOOL_CALL         → 连续的合并为 {"role": "assistant", "tool_calls": [...]}
        - TOOL_RESULT       → {"role": "tool", "tool_call_id": ..., "content": ...}
        - MESSAGE_COMPLETE  → {"role": "assistant", "content": ...}
        - EXTERNAL_MESSAGE  → {"role": "assistant", "name": "<src>", "content": "[来自 ...]"}
                              其他 AI agent 通过 send_message 发来的消息
        - 其他类型跳过
        """
        messages: list[dict] = []
        pending_tool_calls: list[dict] = []

        def _flush_tool_calls():
            nonlocal pending_tool_calls
            if pending_tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": pending_tool_calls,
                })
                pending_tool_calls = []

        for event in events:
            t = event["type"]

            if t == "USER_INPUT":
                _flush_tool_calls()
                from .multimodal import build_user_content
                text = event["data"].get("content", "")
                attachments = event["data"].get("attachments") or []
                messages.append({
                    "role": "user",
                    "content": build_user_content(text, attachments),
                })

            elif t == "tool_call":
                pending_tool_calls.append({
                    "id": event["data"].get("id", ""),
                    "type": "function",
                    "function": {
                        "name": event["data"].get("name", ""),
                        "arguments": _serialize_arguments(event["data"].get("arguments", {})),
                    },
                })

            elif t == "tool_result":
                _flush_tool_calls()
                messages.append({
                    "role": "tool",
                    "tool_call_id": event["data"].get("id", ""),
                    "content": event["data"].get("result", ""),
                })

            elif t == "message_complete":
                _flush_tool_calls()
                messages.append({
                    "role": "assistant",
                    "content": event["data"].get("content", ""),
                })

            elif t == "external_message":
                _flush_tool_calls()
                d = event["data"]
                from_ch = d.get("from_channel", "")
                from_sid = d.get("from_session", "")
                src = f"{from_ch}::{from_sid}" if from_ch or from_sid else "external"
                messages.append({
                    "role": "assistant",
                    "name": _safe_name(src),
                    "content": f"[来自 {src} 的消息] {d.get('content', '')}",
                })

        _flush_tool_calls()
        return messages


def _serialize_arguments(arguments) -> str:
    """将 arguments 序列化为 JSON 字符串（OpenAI tool_calls 要求 string）"""
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments, ensure_ascii=False)


def _safe_name(s: str) -> str:
    """
    把任意字符串规整为 OpenAI 允许的 name（^[a-zA-Z0-9_-]+$，长度<=64）
    例：'ws::sess_c6aa9ad2a883' → 'ws_sess_c6aa9ad2a883'
    """
    cleaned = "".join(c if (c.isalnum() or c in "_-") else "_" for c in s).strip("_")
    return (cleaned or "external")[:64]


def _compute_token_usage(session_id: str, events: list[MessageModel]) -> dict:
    """
    根据事件流计算 token 用量。抽出来便于单测，不依赖 db。

    见 SessionManager.get_token_usage 文档。
    """
    from .token_counter import estimate_event_tokens

    # 倒序找最晚的"携带 usage 的事件"
    anchor_index = -1
    anchor_usage: dict | None = None
    anchor_source: str = ""
    for i in range(len(events) - 1, -1, -1):
        ev = events[i]
        t = ev["type"]
        data = ev.get("data") or {}
        if t == "usage_update":
            usage = data.get("usage")
            if usage:
                anchor_index = i
                anchor_usage = usage
                anchor_source = "usage_update"
                break
        elif t == "done":
            usage = data.get("usage")
            if usage:
                anchor_index = i
                anchor_usage = usage
                anchor_source = "done"
                break

    # 取要估算的事件区间
    if anchor_index >= 0:
        pending_events = events[anchor_index + 1:]
    else:
        pending_events = events

    pending_estimated = sum(estimate_event_tokens(ev) for ev in pending_events)

    if anchor_usage is not None:
        # total_tokens 不存在时回退到 prompt + completion
        total_real = anchor_usage.get("total_tokens")
        if total_real is None:
            total_real = (
                int(anchor_usage.get("prompt_tokens", 0) or 0)
                + int(anchor_usage.get("completion_tokens", 0) or 0)
            )
        anchor_payload = {
            "prompt_tokens": int(anchor_usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(anchor_usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(total_real or 0),
            "at": events[anchor_index]["timestamp"],
            "source": anchor_source,
        }
        total = anchor_payload["total_tokens"] + pending_estimated
    else:
        anchor_payload = None
        total = pending_estimated

    return {
        "session_id": session_id,
        "anchor": anchor_payload,
        "pending_estimated": pending_estimated,
        "total": total,
    }
