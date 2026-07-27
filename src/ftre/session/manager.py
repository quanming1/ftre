"""
SessionManager - 会话与消息持久化（SQLite）

三张表：
- sessions: 会话元信息（id, channel_id, title, created_at, updated_at）
- messages: 聚合消息快照（Msg），一行就是一条 user/assistant/system 消息
- external_sessions: 外部平台会话映射

流式 AgentStreamEvent 只用于实时传输和 trace，不写入 messages。
"""
import json
import time
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

import aiosqlite
from ftre_agent_core.message import Msg

from ftre.config import CONFIG_PATH


class SessionModel(TypedDict):
    """会话元信息"""
    id: str              # 会话唯一标识（含 channel 前缀，如 'ws::sess_xxx'）
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
    timestamp: float     # 排序/分页游标

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
                id            TEXT PRIMARY KEY,
                channel_id    TEXT NOT NULL DEFAULT '',
                title         TEXT NOT NULL DEFAULT '',
                workspace     TEXT NOT NULL DEFAULT '',
                metadata      TEXT NOT NULL DEFAULT '{}',
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id                TEXT PRIMARY KEY,
                session_id        TEXT NOT NULL,
                name              TEXT NOT NULL,
                role              TEXT NOT NULL,
                content           TEXT NOT NULL DEFAULT '[]',
                metadata          TEXT NOT NULL DEFAULT '{}',
                created_at        TEXT NOT NULL,
                usage             TEXT,
                finished_at       TEXT,
                finished_reason   TEXT,
                structured_output TEXT,
                error             TEXT,
                timestamp         REAL NOT NULL
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
        # 索引：channel_id + updated_at（按 channel 过滤会话列表用）
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_channel "
            "ON sessions(channel_id, updated_at DESC)"
        )
        await self._db.commit()

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._db:
            await self._db.close()
            self._db = None

    def create_id(self) -> str:
        """生成新的 session_id"""
        return f"sess_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _row_to_session_model(row) -> SessionModel:
        """把 aiosqlite.Row 转成 SessionModel，安全解析 metadata 列。"""
        raw = row["metadata"] if "metadata" in row.keys() else "{}"
        try:
            metadata = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        return SessionModel(
            id=row["id"],
            channel_id=row["channel_id"],
            title=row["title"],
            workspace=row["workspace"] if "workspace" in row.keys() else "",
            metadata=metadata,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

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
            "INSERT INTO sessions (id, channel_id, title, workspace, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, '{}', ?, ?)",
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
            "INSERT INTO sessions (id, channel_id, title, workspace, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, '{}', ?, ?)",
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
        return self._row_to_session_model(row)

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

    async def get_session_metadata(self, session_id: str) -> dict[str, Any]:
        """读取 session 的完整 metadata（解析后的 dict）。session 不存在返回空 dict。"""
        cursor = await self._db.execute(
            "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return {}
        try:
            return json.loads(row["metadata"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

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
        metadata = await self.get_session_metadata(session_id)
        if value is None:
            metadata.pop(key, None)
        else:
            metadata[key] = value
        now = time.time()
        await self._db.execute(
            "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False), now, session_id),
        )
        await self._db.commit()
        return metadata

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
        return [self._row_to_session_model(r) for r in rows]

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
    # Message（Msg 快照）
    # ============================================================

    async def save_message(
        self,
        session_id: str,
        message: Msg | dict[str, Any],
        *,
        timestamp: float | None = None,
    ) -> str:
        """保存一条完整 Msg；流式 Event 不属于这个存储边界。"""
        msg = message if isinstance(message, Msg) else Msg.model_validate(message)
        payload = msg.model_dump(mode="json")
        now = time.time()
        if timestamp is None:
            try:
                ts = datetime.fromisoformat(msg.created_at).timestamp()
            except (TypeError, ValueError):
                ts = now
        else:
            ts = float(timestamp)
        await self._db.execute(
            """
            INSERT INTO messages (
                id, session_id, name, role, content, metadata, created_at,
                usage, finished_at, finished_reason, structured_output, error,
                timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg.id,
                session_id,
                msg.name,
                msg.role,
                json.dumps(payload["content"], ensure_ascii=False),
                json.dumps(payload["metadata"], ensure_ascii=False),
                msg.created_at,
                _json_or_none(payload.get("usage")),
                msg.finished_at,
                payload.get("finished_reason"),
                _json_or_none(payload.get("structured_output")),
                _json_or_none(payload.get("error")),
                ts,
            ),
        )
        await self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        await self._db.commit()
        return msg.id

    async def update_message(self, message: Msg | dict[str, Any]) -> None:
        """更新已持久化 Msg 的可变快照字段，不改变排序时间。"""
        msg = message if isinstance(message, Msg) else Msg.model_validate(message)
        payload = msg.model_dump(mode="json")
        now = time.time()
        await self._db.execute(
            """
            UPDATE messages
            SET name = ?, role = ?, content = ?, metadata = ?, created_at = ?,
                usage = ?, finished_at = ?, finished_reason = ?,
                structured_output = ?, error = ?
            WHERE id = ?
            """,
            (
                msg.name,
                msg.role,
                json.dumps(payload["content"], ensure_ascii=False),
                json.dumps(payload["metadata"], ensure_ascii=False),
                msg.created_at,
                _json_or_none(payload.get("usage")),
                msg.finished_at,
                payload.get("finished_reason"),
                _json_or_none(payload.get("structured_output")),
                _json_or_none(payload.get("error")),
                msg.id,
            ),
        )
        await self._db.execute(
            """
            UPDATE sessions
            SET updated_at = ?
            WHERE id = (SELECT session_id FROM messages WHERE id = ?)
            """,
            (now, msg.id),
        )
        await self._db.commit()

    async def get_messages_by_session(self, session_id: str) -> list[MessageModel]:
        """获取指定 session 的全部消息（按时间正序）"""
        cursor = await self._db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_message_model(r) for r in rows]

    async def get_recent_messages_by_turns(
        self, session_id: str, limit_turns: int = 5, before_ts: float | None = None
    ) -> tuple[list[MessageModel], bool]:
        """获取指定 session 最近 N 轮对话的所有消息。

        一轮 = 一条可见 user Msg，到下一条可见 user Msg（或末尾）之间的消息。

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

        # 1. 找到最近 limit_turns 个可见 user Msg 中最早的那个的 timestamp
        cursor = await self._db.execute(
            f"""
            SELECT timestamp FROM messages
            WHERE session_id = ?
              {ts_filter}
              AND role = 'user'
              AND (
                json_extract(metadata, '$.hide') IS NULL
                OR json_extract(metadata, '$.hide') IS NOT 1
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

        # 3. 从 earliest_turn_ts 到 before_ts（如有）的所有 Msg
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
        messages = [_row_to_message_model(r) for r in rows]
        has_more = len(messages) < total_in_range
        return messages, has_more

    # ============================================================
    # Token 用量（最近一次 LLM 实算 + 之后未计入消息的字符级粗估）
    # ============================================================

    async def get_token_usage(self, session_id: str) -> dict:
        """
        计算指定 session 当前 token 用量。

        策略：
        - 找 Msg 历史中最晚的"携带 usage 的 assistant Msg"作为 anchor
        - anchor 之后还没被 LLM 计入但会进下次 prompt 的 Msg 用字符级粗估
        - total = anchor.total_tokens + pending_estimated
        - 没有 anchor 时（全新 session）退化为对全量 Msg 估算

        Returns:
            {
              "session_id": str,
              "anchor": {
                "prompt_tokens": int,
                "completion_tokens": int,
                "total_tokens": int,
                "at": float,
                "source": "msg"
              } | None,
              "pending_estimated": int,
              "total": int
            }
        """
        messages = await self.get_messages_by_session(session_id)
        return _compute_token_usage(session_id, messages)

    # ============================================================
    # 历史恢复
    # ============================================================

    # Msg → provider 消息的转换统一位于 converter.py。


def _json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _load_json(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _row_to_message_model(row) -> MessageModel:
    return MessageModel(
        id=row["id"],
        session_id=row["session_id"],
        name=row["name"],
        role=row["role"],
        content=_load_json(row["content"], []),
        metadata=_load_json(row["metadata"], {}),
        created_at=row["created_at"],
        usage=_load_json(row["usage"], None),
        finished_at=row["finished_at"],
        finished_reason=row["finished_reason"],
        structured_output=_load_json(row["structured_output"], None),
        error=_load_json(row["error"], None),
        timestamp=row["timestamp"],
    )


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
    根据 Msg 快照计算 token 用量。抽出来便于单测，不依赖 db。

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
