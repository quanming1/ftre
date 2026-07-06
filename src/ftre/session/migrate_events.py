"""
DB migration：旧事件行 → 新消息行。

旧格式（每事件一行）：
  usage_update, reasoning_complete, assistant_message_complete(str),
  tool_call, tool_result, done, error, user_message, context_compact, external_message

新格式（每消息一行）：
  assistant_message_complete(list[dict] + metadata), tool_result,
  done, user_message, context_compact, external_message

被合并的旧事件：
  usage_update      → assistant_message_complete.metadata.usage
  reasoning_complete → assistant_message_complete.content[].thinking
  tool_call         → assistant_message_complete.content[].toolCall
  assistant_message_complete(str) → assistant_message_complete.content[].text

合并规则：同一轮 LLM 的事件按时间顺序收集，遇到 tool_result / done / user_message / context_compact
时 flush 为一条 assistant_message_complete。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# 旧事件类型 → 需要合并的标志
_MERGED_TYPES = frozenset({
    "usage_update",
    "reasoning_complete",
    "tool_call",
    "assistant_message_complete",  # 旧版 content 是 str
    "error",
})

# 直接透传的类型（不参与合并）
_PASSTHROUGH_TYPES = frozenset({
    "tool_result",
    "done",
    "user_message",
    "context_compact",
    "external_message",
    "retry",
    "reasoning",          # 流式 chunk，不应该在 DB 里但防御性处理
    "assistant_message",  # 流式 chunk
    "tool_call_streaming",
    "message_complete",   # 极旧类型
})

# 旧 assistant_message_complete 的 content 是 str
# 新 assistant_message_complete 的 content 是 list[dict]
def _is_old_amc(data: dict) -> bool:
    """判断 assistant_message_complete 是否为旧格式（content 是 str）。"""
    content = data.get("content")
    return isinstance(content, str)


class _TurnBuffer:
    """收集一轮 LLM 输出的旧事件，合并为一条新 assistant_message_complete。"""

    def __init__(self) -> None:
        self.content: list[dict] = []
        self.usage: dict | None = None
        self.kind: str = "final"
        self.stop_reason: str | None = None
        self.error: dict | None = None
        self.event_ids: list[str] = []
        self.timestamp: float = 0.0

    def has_content(self) -> bool:
        return bool(self.content)

    def add_usage(self, data: dict, ts: float) -> None:
        self.usage = data.get("usage", {})
        self.event_ids.append(data.get("event_id", ""))
        if not self.timestamp:
            self.timestamp = ts

    def add_reasoning(self, data: dict, ts: float) -> None:
        text = data.get("content", "")
        eid = data.get("event_id", "")
        if text:
            self.content.append({"type": "thinking", "thinking": text, "event_id": eid})
        self.event_ids.append(eid)
        if not self.timestamp:
            self.timestamp = ts

    def add_assistant_text(self, data: dict, ts: float) -> None:
        text = data.get("content", "")
        eid = data.get("event_id", "")
        if text:
            self.content.append({"type": "text", "text": text, "event_id": eid})
        kind = data.get("kind", "final")
        self.kind = kind
        self.event_ids.append(eid)
        if not self.timestamp:
            self.timestamp = ts

    def add_tool_call(self, data: dict, ts: float) -> None:
        eid = data.get("event_id", "")
        self.content.append({
            "type": "toolCall",
            "id": data.get("id", ""),
            "name": data.get("name", ""),
            "arguments": data.get("arguments", {}),
            "event_id": eid,
        })
        self.event_ids.append(eid)
        if not self.timestamp:
            self.timestamp = ts

    def add_error(self, data: dict, ts: float) -> None:
        self.error = {"message": data.get("message", ""), "code": data.get("code", "unknown")}
        self.stop_reason = "error"
        self.event_ids.append(data.get("event_id", ""))
        if not self.timestamp:
            self.timestamp = ts

    def to_message(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {"kind": self.kind}
        if self.usage:
            metadata["usage"] = self.usage
        if self.stop_reason:
            metadata["stopReason"] = self.stop_reason
        if self.error:
            metadata["error"] = self.error
        return {
            "type": "assistant_message_complete",
            "data": {
                "content": self.content,
                "metadata": metadata,
                "event_id": self.event_ids[0] if self.event_ids else "",
            },
            "timestamp": self.timestamp,
        }


def _coalesce_session_rows(rows: list[dict]) -> list[dict[str, Any]]:
    """把一个 session 的旧事件行合并为新消息行。

    rows: [{"id":..., "session_id":..., "type":..., "data":...(json str), "timestamp":...}, ...]
    返回: [{"type":..., "data":...(dict), "timestamp":...}, ...]
    """
    result: list[dict[str, Any]] = []
    buf: _TurnBuffer | None = None

    def flush():
        nonlocal buf
        if buf is not None and buf.has_content():
            result.append(buf.to_message())
        buf = None

    for row in rows:
        t = row["type"]
        data = json.loads(row["data"]) if row["data"] else {}
        ts = row["timestamp"]

        if t == "usage_update":
            if buf is None:
                buf = _TurnBuffer()
            buf.add_usage(data, ts)

        elif t == "reasoning_complete":
            if buf is None:
                buf = _TurnBuffer()
            buf.add_reasoning(data, ts)

        elif t == "assistant_message_complete":
            if _is_old_amc(data):
                # 旧格式 → 合并到 buffer
                if buf is None:
                    buf = _TurnBuffer()
                buf.add_assistant_text(data, ts)
            else:
                # 新格式（已经是 list[dict] + metadata）→ 直接透传
                flush()
                result.append({"type": t, "data": data, "timestamp": ts})

        elif t == "tool_call":
            if buf is None:
                buf = _TurnBuffer()
            buf.add_tool_call(data, ts)

        elif t == "error":
            if buf is None:
                buf = _TurnBuffer()
            buf.add_error(data, ts)

        elif t == "tool_result":
            flush()
            result.append({"type": t, "data": data, "timestamp": ts})

        elif t == "done":
            flush()
            # done 旧格式可能有 usage，新格式没有
            done_data = dict(data)
            done_data.pop("usage", None)
            result.append({"type": t, "data": done_data, "timestamp": ts})

        elif t in ("user_message", "context_compact", "external_message", "retry"):
            flush()
            result.append({"type": t, "data": data, "timestamp": ts})

        else:
            # 未知类型（message_complete 等极旧类型）→ 跳过
            logger.debug(f"[migrate] 跳过未知事件类型: {t}")

    flush()
    return result


async def migrate_events_to_messages(db) -> None:
    """检测并迁移旧事件格式到新消息格式。

    在 SessionManager.init() 中调用。幂等：如果没有旧事件则直接返回。
    """
    # 检测是否需要迁移
    cursor = await db.execute("""
        SELECT COUNT(*) as cnt FROM messages
        WHERE type IN ('usage_update', 'reasoning_complete', 'tool_call')
    """)
    count = (await cursor.fetchone())["cnt"]
    if count == 0:
        logger.info("[migrate] 无旧事件数据，跳过迁移")
        return

    logger.info(f"[migrate] 检测到 {count} 行旧事件数据，开始迁移...")

    # 创建新表
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS messages_new (
            id          TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            type        TEXT NOT NULL,
            data        TEXT NOT NULL DEFAULT '{}',
            timestamp   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_new_session
            ON messages_new(session_id, timestamp ASC);
    """)

    # 获取所有 session_id
    cursor = await db.execute("SELECT DISTINCT session_id FROM messages")
    session_ids = [r["session_id"] for r in await cursor.fetchall()]

    total_old = 0
    total_new = 0
    for sid in session_ids:
        cursor = await db.execute(
            "SELECT id, session_id, type, data, timestamp FROM messages "
            "WHERE session_id = ? ORDER BY timestamp ASC",
            (sid,),
        )
        rows = await cursor.fetchall()
        total_old += len(rows)

        coalesced = _coalesce_session_rows(rows)

        for i, msg in enumerate(coalesced):
            msg_id = uuid.uuid4().hex[:16]
            await db.execute(
                "INSERT INTO messages_new (id, session_id, type, data, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    msg_id,
                    sid,
                    msg["type"],
                    json.dumps(msg["data"], ensure_ascii=False),
                    msg["timestamp"],
                ),
            )
            total_new += 1

    # 替换表
    await db.executescript("""
        DROP TABLE messages;
        ALTER TABLE messages_new RENAME TO messages;
    """)

    # 重建索引（DROP TABLE 会连带删除旧索引，但 ALTER TABLE RENAME 不会重命名索引）
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_session "
        "ON messages(session_id, timestamp ASC)"
    )

    await db.commit()
    logger.info(
        f"[migrate] 迁移完成：{total_old} 行旧事件 → {total_new} 行新消息 "
        f"({len(session_ids)} sessions)"
    )
