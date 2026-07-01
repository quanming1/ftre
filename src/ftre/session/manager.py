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

from ftre_agent_core.agent.event import (
    AgentEvent,
    AssistantMessageCompleteEvent,
    ReasoningCompleteEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserMessageEvent,
)
from ftre_agent_core.reasoning import format_assistant_message
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
    type: str            # 事件类型（user_message / TOOL_CALL / TOOL_RESULT / MESSAGE_COMPLETE / ...）
    data: dict[str, Any] # 事件数据（JSON）
    timestamp: float     # 事件时间戳

class ExternalSessionModel(TypedDict):
    channel_id: str
    external_key: str
    session_id: str
    external_data: dict[str, Any]
    created_at: float
    updated_at: float


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

            CREATE TABLE IF NOT EXISTS external_sessions (
                channel_id    TEXT NOT NULL,
                external_key  TEXT NOT NULL,
                session_id    TEXT NOT NULL,
                external_data TEXT NOT NULL DEFAULT '{}',
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL,
                PRIMARY KEY (channel_id, external_key)
            );

            CREATE INDEX IF NOT EXISTS idx_external_sessions_session
                ON external_sessions(session_id);
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
        await self._migrate_backfill_message_event_id()
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

    async def _migrate_backfill_message_event_id(self) -> None:
        """Backfill legacy rows so HTTP history and WS replay share an event id."""
        await self._db.execute(
            """
            UPDATE messages
            SET data = json_set(data, '$.event_id', id)
            WHERE json_valid(data)
              AND (
                json_extract(data, '$.event_id') IS NULL
                OR json_extract(data, '$.event_id') = ''
              )
            """
        )

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

    async def get_or_create_external_session(
        self,
        channel_id: str,
        external_key: str,
        title: str = "",
        workspace: str = "",
        external_data: dict[str, Any] | None = None,
    ) -> str:
        """Get or create a local session bound to an external platform conversation."""
        if not channel_id:
            raise ValueError("channel_id cannot be empty")
        if not external_key:
            raise ValueError("external_key cannot be empty")

        cursor = await self._db.execute(
            """
            SELECT es.session_id
            FROM external_sessions es
            JOIN sessions s ON s.id = es.session_id
            WHERE es.channel_id = ? AND es.external_key = ?
            """,
            (channel_id, external_key),
        )
        row = await cursor.fetchone()
        now = time.time()
        serialized = json.dumps(external_data or {}, ensure_ascii=False)
        if row:
            session_id = row["session_id"]
            await self._db.execute(
                """
                UPDATE external_sessions
                SET updated_at = ?, external_data = ?
                WHERE channel_id = ? AND external_key = ?
                """,
                (now, serialized, channel_id, external_key),
            )
            await self._db.commit()
            return session_id

        await self._db.execute(
            "DELETE FROM external_sessions WHERE channel_id = ? AND external_key = ?",
            (channel_id, external_key),
        )

        session_id = f"{channel_id}::{self.create_id()}"
        await self._db.execute(
            "INSERT INTO sessions (id, channel_id, title, workspace, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, channel_id, title, workspace, now, now),
        )
        await self._db.execute(
            """
            INSERT INTO external_sessions (
                channel_id, external_key, session_id, external_data, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (channel_id, external_key, session_id, serialized, now, now),
        )
        await self._db.commit()
        return session_id

    async def get_external_session(self, session_id: str) -> ExternalSessionModel | None:
        """Look up external platform conversation metadata by local session id."""
        cursor = await self._db.execute(
            "SELECT * FROM external_sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            external_data = json.loads(row["external_data"] or "{}")
        except json.JSONDecodeError:
            external_data = {}
        return ExternalSessionModel(
            channel_id=row["channel_id"],
            external_key=row["external_key"],
            session_id=row["session_id"],
            external_data=external_data,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

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

    async def save_message(
        self,
        session_id: str,
        type: str,
        data: dict[str, Any],
        *,
        timestamp: float | None = None,
    ) -> str:
        """
        保存一条消息/事件到指定 session。
        同时更新 session 的 updated_at。
        返回生成的 message id。

        timestamp 可选：传入则消息行用该值（用于把 context_compact 等"游标"事件
        插到历史中间——按 ASC 排序时排在某个边界事件之前）。session.updated_at
        始终用真实当前时间，不会因游标回插而错乱会话列表排序。
        """
        msg_id = uuid.uuid4().hex[:16]
        stored_data = dict(data or {})
        if not stored_data.get("event_id"):
            stored_data["event_id"] = msg_id
        now = time.time()
        ts = now if timestamp is None else float(timestamp)
        await self._db.execute(
            "INSERT INTO messages (id, session_id, type, data, timestamp) VALUES (?, ?, ?, ?, ?)",
            (msg_id, session_id, type, json.dumps(stored_data, ensure_ascii=False), ts),
        )
        await self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        await self._db.commit()
        return msg_id

    async def update_message_data(self, message_id: str, data: dict[str, Any]) -> None:
        """更新一条事件的 data，不改变 timestamp。"""
        now = time.time()
        await self._db.execute(
            "UPDATE messages SET data = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), message_id),
        )
        await self._db.execute(
            """
            UPDATE sessions
            SET updated_at = ?
            WHERE id = (SELECT session_id FROM messages WHERE id = ?)
            """,
            (now, message_id),
        )
        await self._db.commit()

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

    async def get_recent_messages_by_turns(
        self, session_id: str, limit_turns: int = 5, before_ts: float | None = None
    ) -> tuple[list[MessageModel], bool]:
        """获取指定 session 最近 N 轮对话的所有消息。

        一轮 = 一个 type='user_message' 且 metadata.hide != true 的事件，
        到下一个可见 user_message（或末尾）之间的所有事件。

        Args:
            limit_turns: 返回最近 N 轮
            before_ts: 可选游标，只考虑 timestamp < before_ts 的事件（用于加载更早）

        Returns:
            (messages, has_more): messages 按时间正序；has_more 表示是否还有更早的消息。
        """
        ts_filter = "AND timestamp < ?" if before_ts is not None else ""
        params = [session_id]
        if before_ts is not None:
            params.append(before_ts)

        # 1. 找到最近 limit_turns 个可见 user_message 中最早的那个的 timestamp
        cursor = await self._db.execute(
            f"""
            SELECT timestamp FROM messages
            WHERE session_id = ?
              {ts_filter}
              AND type = 'user_message'
              AND (
                json_extract(data, '$.metadata.hide') IS NULL
                OR json_extract(data, '$.metadata.hide') IS NOT 1
              )
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (*params, limit_turns),
        )
        turn_rows = await cursor.fetchall()

        if not turn_rows:
            # 没有可见 user_message，返回空
            return [], False

        earliest_turn_ts = turn_rows[-1]["timestamp"]

        # 2. 查 total count（考虑 before_ts 过滤）判断 has_more
        count_params = [session_id]
        count_filter = ""
        if before_ts is not None:
            count_filter = "AND timestamp < ?"
            count_params.append(before_ts)
        cursor = await self._db.execute(
            f"SELECT COUNT(*) as cnt FROM messages WHERE session_id = ? {count_filter}",
            count_params,
        )
        total_in_range = (await cursor.fetchone())["cnt"]

        # 3. 从 earliest_turn_ts 到 before_ts（如有）的所有事件
        end_filter = "AND timestamp < ?" if before_ts is not None else ""
        end_params = [session_id, earliest_turn_ts] + ([before_ts] if before_ts is not None else [])
        cursor = await self._db.execute(
            f"""
            SELECT * FROM messages
            WHERE session_id = ? AND timestamp >= ?
            {end_filter}
            ORDER BY timestamp ASC
            """,
            end_params,
        )
        rows = await cursor.fetchall()
        messages = [
            MessageModel(
                id=r["id"],
                session_id=r["session_id"],
                type=r["type"],
                data=json.loads(r["data"]),
                timestamp=r["timestamp"],
            )
            for r in rows
        ]
        has_more = len(messages) < total_in_range
        return messages, has_more

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
        prune: dict | None = None,
    ) -> list[dict]:
        """
        将事件流重建为 OpenAI 格式消息列表。

        config 可传入当前模型配置；当 config["llm"]["vision"] 为 false 时，
        历史用户消息里的图片附件会被降级成文本提示。

        prune 参数（L1 工具输出修剪，对喂给 LLM 的视图生效，不改 DB）：
        - prune={"protect_turns": 2, "max_chars": 2000, "head_chars": 1000, "tail_chars": 1000}
        - 最近 protect_turns 个可见 user_message 之内的 tool_result 不截断（AI 最近动作要看到全文）
        - 之外的 tool_result：单条超过 max_chars 就截断为 head_chars + 占位 + tail_chars
        - error 非空的失败结果不截断（通常很短且关键）
        - 历史回放（GET /messages）等场景不传 prune，保留完整原文给前端

        转换规则：
        - user_message        → {"role": "user", "content": ...}
        - TOOL_CALL           → 连续的合并为 {"role": "assistant", "tool_calls": [...]}
        - TOOL_RESULT         → {"role": "tool", "tool_call_id": ..., "content": ...}
        - MESSAGE_COMPLETE    → {"role": "assistant", "content": ...}
        - REASONING_COMPLETE  → 暂存并合并到下一条 assistant message 的 reasoning_content
        - EXTERNAL_MESSAGE    → {"role": "assistant", "name": "<src>", "content": "[来自 ...]"}
                                其他 AI agent 通过 send_message 发来的消息
        - 其他类型跳过
        """
        messages: list[dict] = []
        pending_tool_calls: list[dict] = []
        pending_reasoning: str | None = None
        pending_assistant_content: Any = None
        pending_assistant_reasoning: str | None = None
        llm_config = (config or {}).get("llm") or {}
        include_images = bool(llm_config.get("vision", False))
        # ─── L1 prune 预处理：算出"受保护索引集"（最近 N 个可见 user_message 内）───
        # 受保护索引内的 tool_result 不修剪。这之外的 tool_result 单条超 max_chars 就截断。
        prune_protected: set[int] = set()
        prune_max_chars = 0
        prune_head_chars = 0
        prune_tail_chars = 0
        if prune:
            prune_max_chars = int(prune.get("max_chars", 2000) or 0)
            prune_head_chars = int(prune.get("head_chars", 1000) or 0)
            prune_tail_chars = int(prune.get("tail_chars", 1000) or 0)
            protect_turns = int(prune.get("protect_turns", 2) or 0)
            if prune_max_chars > 0 and protect_turns > 0:
                # 倒序扫，遇到第 protect_turns 个可见 user_message 即停。其后的所有索引都受保护。
                seen_user = 0
                for i in range(len(events) - 1, -1, -1):
                    prune_protected.add(i)
                    if (
                        events[i]["type"] == "user_message"
                        and not ((events[i].get("data") or {}).get("metadata") or {}).get("hide", False)
                    ):
                        seen_user += 1
                        if seen_user >= protect_turns:
                            break

        def _maybe_prune_tool_result(idx: int, result: str, error) -> str:
            """对 tool_result 做 head/tail 截断（只对超长且不在保护区内的）。"""
            if not prune_max_chars or idx in prune_protected:
                return result
            if error:  # 失败结果通常很短且关键，不截
                return result
            if not isinstance(result, str) or len(result) <= prune_max_chars:
                return result
            cut = len(result) - prune_head_chars - prune_tail_chars
            head = result[:prune_head_chars]
            tail = result[-prune_tail_chars:] if prune_tail_chars else ""
            return f"{head}\n…[L1 修剪 {cut} 字符]…\n{tail}"

        def _take_reasoning() -> str | None:
            nonlocal pending_reasoning
            text = pending_reasoning
            pending_reasoning = None
            return text

        def _coerce_user_message_content(content: Any) -> str | list[dict]:
            from .multimodal import normalize_user_content

            return normalize_user_content(content, include_images=include_images)

        def _flush_pending_assistant():
            nonlocal pending_assistant_content, pending_assistant_reasoning
            if pending_assistant_content is None:
                return
            msg = format_assistant_message(
                content=pending_assistant_content,
                reasoning=pending_assistant_reasoning,
            )
            messages.append(msg)
            pending_assistant_content = None
            pending_assistant_reasoning = None

        def _flush_tool_calls():
            nonlocal pending_tool_calls, pending_assistant_content, pending_assistant_reasoning
            if pending_tool_calls:
                if pending_assistant_content is not None:
                    msg = format_assistant_message(
                        content=pending_assistant_content,
                        reasoning=pending_assistant_reasoning,
                        tool_calls=pending_tool_calls,
                    )
                    pending_assistant_content = None
                    pending_assistant_reasoning = None
                else:
                    msg = format_assistant_message(
                        reasoning=_take_reasoning(),
                        tool_calls=pending_tool_calls,
                    )
                messages.append(msg)
                pending_tool_calls = []
            else:
                _flush_pending_assistant()

        for idx, event in enumerate(events):
            # 尝试转为 AgentEvent class（Agent 事件用类型安全方式访问）
            _ae: AgentEvent | None = None
            _t = event["type"]
            if _t not in ("context_compact", "external_message"):
                try:
                    _ae = AgentEvent.from_dict(event)
                except (KeyError, ValueError):
                    _ae = None

            if isinstance(_ae, ReasoningCompleteEvent):
                _flush_pending_assistant()
                # 一轮 LLM 思考的完整文本，挂到下一条 assistant message 上
                pending_reasoning = _ae.content or None

            elif isinstance(_ae, ToolCallEvent):
                pending_tool_calls.append({
                    "id": _ae.tool_id,
                    "type": "function",
                    "function": {
                        "name": _ae.tool_name,
                        "arguments": _serialize_arguments(_ae.arguments),
                    },
                })

            elif isinstance(_ae, ToolResultEvent):
                _flush_tool_calls()
                result_content = _ae.result
                error = _ae.error
                if prune_max_chars > 0:
                    result_content = _maybe_prune_tool_result(idx, result_content, error)
                messages.append({
                    "role": "tool",
                    "tool_call_id": _ae.tool_id,
                    "content": result_content,
                })

            elif isinstance(_ae, AssistantMessageCompleteEvent):
                _flush_tool_calls()
                pending_assistant_content = _ae.content
                pending_assistant_reasoning = _take_reasoning()

            elif isinstance(_ae, UserMessageEvent):
                _flush_tool_calls()
                _take_reasoning()
                data = event["data"]
                content = _ae.content
                attachments = data.get("attachments") or []
                if attachments:
                    from .multimodal import build_user_content
                    content = build_user_content(
                        content,
                        attachments,
                        include_images=include_images,
                    )
                messages.append({
                    "role": "user",
                    "content": _coerce_user_message_content(content),
                })

            elif _t == "external_message":
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

            elif _t == "context_compact":
                # 压缩事件 = 游标。按事件顺序处理：
                # - 清空之前累积的 messages，注入 summary 作为新起点；
                # - 该事件之后的事件照常重建 → 自动形成 tail（最近原文）。
                # - 多条 context_compact 时后一条会再次清空 messages，等价于"以最后
                #   一条为准 + 保留其后 tail"。
                # - enabled=false 的 pending 压缩事件只是预生成摘要，不参与上下文重建。
                data = event["data"] or {}
                if data.get("enabled", True) is not True:
                    continue
                _flush_tool_calls()
                _take_reasoning()
                summary = data.get("summary", "")
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
    from ftre_agent_core.agent.event import AgentEvent, UsageUpdateEvent, DoneEvent
    for i in range(len(events) - 1, -1, -1):
        ev = events[i]
        _ae = None
        try:
            _ae = AgentEvent.from_dict(ev)
        except (KeyError, ValueError):
            pass
        if not isinstance(_ae, (UsageUpdateEvent, DoneEvent)):
            continue
        usage = None
        if isinstance(_ae, UsageUpdateEvent):
            usage = _ae.usage
        elif isinstance(_ae, DoneEvent) and _ae.usage:
            usage = _ae.usage
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
