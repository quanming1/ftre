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
    workspace: str       # 当前工作区绝对路径（cwd 来源；为空表示未设置）
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
                workspace   TEXT NOT NULL DEFAULT '',
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
        # 老库迁移：sessions 表存量没有这些列时补上
        await self._migrate_add_column(
            "sessions", "channel_id", "TEXT NOT NULL DEFAULT ''"
        )
        await self._migrate_add_column(
            "sessions", "workspace", "TEXT NOT NULL DEFAULT ''"
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

    async def create_session(
        self, channel_id: str, title: str = "", workspace: str = ""
    ) -> str:
        """创建新 session，返回带 channel_id 前缀的 session_id（格式: '{channel_id}::sess_xxx'）"""
        if not channel_id:
            raise ValueError("channel_id 不能为空")
        sid = f"{channel_id}::{self.create_id()}"
        now = time.time()
        await self._db.execute(
            "INSERT INTO sessions (id, channel_id, title, workspace, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, channel_id, title, workspace, now, now),
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
            workspace=row["workspace"] if "workspace" in row.keys() else "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

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
        now = time.time()
        sets: list[str] = []
        params: list = []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if workspace is not None:
            sets.append("workspace = ?")
            params.append(workspace)
        sets.append("updated_at = ?")
        params.append(now)
        params.append(session_id)
        sql = f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?"
        await self._db.execute(sql, tuple(params))
        await self._db.commit()

    async def delete_session(self, session_id: str) -> None:
        """删除 session 及其所有 messages"""
        await self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self._db.commit()

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
        conditions: list[str] = []
        params: list = []
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        if workspace is not None:
            conditions.append("workspace = ?")
            params.append(workspace)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])
        cursor = await self._db.execute(
            f"SELECT * FROM sessions {where} "
            "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            tuple(params),
        )
        rows = await cursor.fetchall()
        return [
            SessionModel(
                id=r["id"],
                channel_id=r["channel_id"],
                title=r["title"],
                workspace=r["workspace"] if "workspace" in r.keys() else "",
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    async def count_sessions(
        self,
        channel_id: str | None = None,
        workspace: str | None = None,
    ) -> int:
        """返回 sessions 总数（用于分页 total）"""
        conditions: list[str] = []
        params: list = []
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        if workspace is not None:
            conditions.append("workspace = ?")
            params.append(workspace)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT COUNT(*) AS n FROM sessions {where}",
            tuple(params),
        )
        row = await cursor.fetchone()
        return int(row["n"]) if row else 0

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
        conditions: list[str] = []
        params: list = []
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT workspace, COUNT(*) AS n, MAX(updated_at) AS latest "
            f"FROM sessions {where} "
            "GROUP BY workspace ORDER BY latest DESC",
            tuple(params),
        )
        rows = await cursor.fetchall()
        return [
            {
                "workspace": r["workspace"] or "",
                "session_count": int(r["n"]),
                "latest_at": r["latest"] or 0,
            }
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
    def to_openai_messages(
        events: list[MessageModel],
        *,
        config: dict | None = None,
    ) -> list[dict]:
        """
        将事件流重建为 OpenAI 格式消息列表。

        config 可传入当前模型配置；当 config["llm"]["vision"] 为 false 时，
        历史用户消息里的图片附件会被降级成文本提示。

        转换规则：
        - USER_INPUT          → {"role": "user", "content": ...}
        - TOOL_CALL           → 连续的合并为 {"role": "assistant", "tool_calls": [...]}
        - TOOL_RESULT         → {"role": "tool", "tool_call_id": ..., "content": ...}
        - MESSAGE_COMPLETE    → {"role": "assistant", "content": ...}
        - REASONING_COMPLETE  → 暂存到下一条 assistant message 的 reasoning_content
                                （部分 thinking 模型要求多轮间透传）
        - EXTERNAL_MESSAGE    → {"role": "assistant", "name": "<src>", "content": "[来自 ...]"}
                                其他 AI agent 通过 send_message 发来的消息
        - 其他类型跳过
        """
        messages: list[dict] = []
        pending_tool_calls: list[dict] = []
        pending_reasoning: str | None = None
        llm_config = (config or {}).get("llm") or {}
        include_images = bool(llm_config.get("vision", True))

        def _take_reasoning() -> str | None:
            nonlocal pending_reasoning
            text = pending_reasoning
            pending_reasoning = None
            return text

        def _flush_tool_calls():
            nonlocal pending_tool_calls
            if pending_tool_calls:
                msg: dict = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": pending_tool_calls,
                }
                reasoning = _take_reasoning()
                if reasoning:
                    msg["reasoning_content"] = reasoning
                messages.append(msg)
                pending_tool_calls = []

        for event in events:
            t = event["type"]

            if t == "USER_INPUT":
                _flush_tool_calls()
                # user 消息边界：丢弃可能残留的 reasoning
                _take_reasoning()
                from .multimodal import build_user_content
                content = event["data"].get("content", "")
                attachments = event["data"].get("attachments") or []
                messages.append({
                    "role": "user",
                    "content": build_user_content(
                        content,
                        attachments,
                        include_images=include_images,
                    ),
                })

            elif t == "reasoning_complete":
                # 一轮 LLM 思考的完整文本，挂到下一条 assistant message 上
                pending_reasoning = event["data"].get("content", "") or None

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
                msg: dict = {
                    "role": "assistant",
                    "content": event["data"].get("content", ""),
                }
                reasoning = _take_reasoning()
                if reasoning:
                    msg["reasoning_content"] = reasoning
                messages.append(msg)

            elif t == "external_message":
                _flush_tool_calls()
                _take_reasoning()
                d = event["data"]
                from_ch = d.get("from_channel", "")
                from_sid = d.get("from_session", "")
                src = f"{from_ch}::{from_sid}" if from_ch or from_sid else "external"
                messages.append({
                    "role": "assistant",
                    "name": _safe_name(src),
                    "content": f"[来自 {src} 的消息] {d.get('content', '')}",
                })

            elif t == "context_compact":
                # 压缩事件：丢弃之前所有 messages，用 summary 作为新起点
                _flush_tool_calls()
                _take_reasoning()
                summary = (event["data"] or {}).get("summary", "")
                messages = []
                if summary:
                    messages.append({
                        "role": "user",
                        "content": f"[历史上下文摘要]\n{summary}",
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


def _find_anchor(events: list[MessageModel]) -> tuple[int, dict | None, str]:
    """倒序找最晚的"携带 usage 的事件"，返回 (index, usage_dict, source)"""
    for i in range(len(events) - 1, -1, -1):
        ev = events[i]
        if ev["type"] not in ("usage_update", "done"):
            continue
        usage = (ev.get("data") or {}).get("usage")
        if usage:
            return i, usage, ev["type"]
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


def _compute_token_usage(session_id: str, events: list[MessageModel]) -> dict:
    """
    根据事件流计算 token 用量。抽出来便于单测，不依赖 db。

    见 SessionManager.get_token_usage 文档。
    """
    from .token_counter import estimate_events_tokens

    anchor_index, anchor_usage, anchor_source = _find_anchor(events)

    # 锚点之后的事件用字符级粗估（无锚点时即全量估算）
    pending_events = events[anchor_index + 1:] if anchor_index >= 0 else events
    pending_estimated = estimate_events_tokens(pending_events)

    if anchor_usage is None:
        return {
            "session_id": session_id,
            "anchor": None,
            "pending_estimated": pending_estimated,
            "total": pending_estimated,
        }

    anchor = _build_anchor_payload(
        anchor_usage, events[anchor_index]["timestamp"], anchor_source
    )
    return {
        "session_id": session_id,
        "anchor": anchor,
        "pending_estimated": pending_estimated,
        "total": anchor["total_tokens"] + pending_estimated,
    }
