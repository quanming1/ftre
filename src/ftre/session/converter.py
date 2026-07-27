"""消息格式转换层。

职责：直接把 ftre 的 events → 各 LLM provider 的 API 格式。
不加中间协议，不做持久化，不碰 DB。

    events → to_openai(events)       → OpenAI messages[]
    events → to_anthropic(events)    → Anthropic messages[]  （将来）

变换逻辑（compacted 占位符）是 provider 无关的，
提取为共享预处理函数，各 to_xxx() 复用。

新事件协议（AgentScope 对齐）：
  - 事件类型为大写字符串（TEXT_BLOCK_START, REPLY_END, TOOL_RESULT_END, ...）
  - 同一 reply_id 的事件通过 Msg.append_event 重建为 Msg，再 to_openai_message
  - user_message / external_message / context_compact 保持旧逻辑（非 AgentScope 事件）

旧事件协议（向后兼容）：
  - assistant_message_complete / tool_result 仍支持（老 DB 数据）
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from ftre.session.manager import MessageModel
from ftre.session.multimodal import build_user_content, normalize_user_content

from ftre_agent_core.event import (
    ReplyStartEvent,
    ReplyEndEvent,
    ModelCallStartEvent,
    ModelCallEndEvent,
    TextBlockStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    DataBlockStartEvent,
    DataBlockDeltaEvent,
    DataBlockEndEvent,
    ThinkingBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    HintBlockEvent,
    ToolCallStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    CustomEvent,
    RetryEvent,
    ExceedMaxItersEvent,
)
from ftre_agent_core.message import (
    Msg,
    AssistantMsg,
    to_openai_message,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 事件类型 → 事件类映射（用于反序列化 DB 行为事件对象）
# ═══════════════════════════════════════════════════════════════════

_EVENT_CLASS_MAP: dict[str, type] = {
    "REPLY_START": ReplyStartEvent,
    "REPLY_END": ReplyEndEvent,
    "MODEL_CALL_START": ModelCallStartEvent,
    "MODEL_CALL_END": ModelCallEndEvent,
    "TEXT_BLOCK_START": TextBlockStartEvent,
    "TEXT_BLOCK_DELTA": TextBlockDeltaEvent,
    "TEXT_BLOCK_END": TextBlockEndEvent,
    "DATA_BLOCK_START": DataBlockStartEvent,
    "DATA_BLOCK_DELTA": DataBlockDeltaEvent,
    "DATA_BLOCK_END": DataBlockEndEvent,
    "THINKING_BLOCK_START": ThinkingBlockStartEvent,
    "THINKING_BLOCK_DELTA": ThinkingBlockDeltaEvent,
    "THINKING_BLOCK_END": ThinkingBlockEndEvent,
    "HINT_BLOCK": HintBlockEvent,
    "TOOL_CALL_START": ToolCallStartEvent,
    "TOOL_CALL_DELTA": ToolCallDeltaEvent,
    "TOOL_CALL_END": ToolCallEndEvent,
    "TOOL_RESULT_START": ToolResultStartEvent,
    "TOOL_RESULT_TEXT_DELTA": ToolResultTextDeltaEvent,
    "TOOL_RESULT_DATA_DELTA": ToolResultDataDeltaEvent,
    "TOOL_RESULT_END": ToolResultEndEvent,
    "CUSTOM": CustomEvent,
    "retry": RetryEvent,
    "EXCEED_MAX_ITERS": ExceedMaxItersEvent,
}

# AgentScope 事件类型集合（用于判断是否走新协议重建路径）
_AGENTSCOPE_TYPES = frozenset(_EVENT_CLASS_MAP.keys())

# 不产出 OpenAI 消息的事件类型（生命周期/元数据事件，跳过）
_SKIP_TYPES = frozenset({
    "REPLY_START",
    "MODEL_CALL_START",
    "MODEL_CALL_END",
    "RETRY",
    "EXCEED_MAX_ITERS",
    "CUSTOM",
})


def _deserialize_event(event_row: MessageModel) -> Any:
    """把 DB 事件行反序列化为事件对象。

    DB 的 data 列存的是 model_dump(mode="json") 的完整事件 dict（含 type/id/created_at/...）。
    用 type 字段查找对应的事件类，用 **data 构造实例。
    """
    data = event_row.get("data") or {}
    ev_type = data.get("type") or event_row.get("type", "")
    cls = _EVENT_CLASS_MAP.get(ev_type)
    if cls is None:
        return None
    try:
        return cls(**data)
    except Exception:
        logger.debug("[converter] 反序列化事件失败 type=%s", ev_type, exc_info=True)
        return None


def _rebuild_msg_from_events(events: list[MessageModel]) -> list[dict]:
    """把同一 reply_id 的事件组重建为 Msg，再转成 OpenAI message。

    返回 0 或 1 条 OpenAI 消息（assistant 或 tool）。
    """
    # 找 ReplyStart 事件获取 name
    name = "assistant"
    for ev in events:
        data = ev.get("data") or {}
        if data.get("type") == "REPLY_START":
            name = data.get("name", "assistant")
            break

    msg = AssistantMsg(name=name, content=[], id=events[0].get("reply_id", "") or uuid.uuid4().hex[:16])

    for ev in events:
        event_obj = _deserialize_event(ev)
        if event_obj is None:
            continue
        try:
            msg.append_event(event_obj)
        except Exception:
            logger.debug("[converter] append_event 失败 type=%s", event_row_type(ev), exc_info=True)

    # 如果 Msg 没有内容块，跳过
    if not msg.content:
        return []

    # to_openai_message 会自动处理 ToolResultBlock → role=tool 消息
    result = to_openai_message(msg.content, role=msg.role)
    return [result]


def event_row_type(ev: MessageModel) -> str:
    """从事件行提取类型字符串。"""
    data = ev.get("data") or {}
    return data.get("type") or ev.get("type", "")


# ═══════════════════════════════════════════════════════════════════
# 共享预处理（provider 无关的变换）
# ═══════════════════════════════════════════════════════════════════

def _scan_compacted_ids(events: list[MessageModel]) -> set[str]:
    """预扫描：收集所有 fast compact 标记的 event id。"""
    compacted_ids: set[str] = set()
    for event in events:
        if event["type"] != "context_compact":
            continue
        d = event.get("data") or {}
        if d.get("mode") == "fast":
            compacted_ids.update(d.get("events", []))
    return compacted_ids


# ═══════════════════════════════════════════════════════════════════
# OpenAI Chat Completions 格式
# ═══════════════════════════════════════════════════════════════════

def to_openai(
    events: list[MessageModel],
    *,
    config: dict | None = None,
) -> list[dict]:
    """ftre events → OpenAI Chat Completions messages[]。

    包含 provider 无关的变换（compacted 占位符）。
    """
    llm_config = (config or {}).get("llm") or {}
    include_images = bool(llm_config.get("vision", False))

    # 共享预处理
    compacted_ids = _scan_compacted_ids(events)

    fast_hint_inserted = False
    messages: list[dict] = []

    # 按 reply_id 分组收集 AgentScope 事件
    # reply_id → list[event indices]
    reply_groups: dict[str, list[int]] = {}
    # 记录每个 reply_id 第一次出现的位置（保持输出顺序）
    reply_order: list[str] = []

    for idx, event in enumerate(events):
        _t = event["type"]
        data = event.get("data") or {}

        # ── user_message ──
        if _t == "user_message":
            content = data.get("content", "")
            attachments = data.get("attachments") or []
            if attachments:
                content = build_user_content(
                    content, attachments, include_images=include_images,
                )
            messages.append({
                "role": "user",
                "content": normalize_user_content(content, include_images=include_images),
            })
            continue

        # ── external_message ──
        if _t == "external_message":
            from_ch = data.get("from_channel", "")
            from_sid = data.get("from_session", "")
            src = f"{from_ch}::{from_sid}" if from_ch or from_sid else "external"
            messages.append({
                "role": "assistant",
                "name": _safe_name(src),
                "content": f"[来自 {src} 的消息] {data.get('content', '')}",
            })
            continue

        # ── context_compact ──
        if _t == "context_compact":
            mode = data.get("mode", "summary")
            if mode == "fast":
                if not fast_hint_inserted:
                    messages.append({
                        "role": "user",
                        "content": "<FTRE_COMPACT_NOTICE>Prior tool outputs have been fast-compacted to placeholders. Re-invoke the relevant tools if you need their actual content.</FTRE_COMPACT_NOTICE>",
                    })
                    fast_hint_inserted = True
                continue
            # summary：清空之前所有消息，注入摘要
            messages = []
            summary = data.get("summary", "")
            if summary:
                messages.append({
                    "role": "user",
                    "content": f"[历史上下文摘要]\n{summary}",
                })
            continue

        # ── 旧协议兼容：assistant_message_complete ──
        if _t == "assistant_message_complete":
            blocks = data.get("content", [])
            # fast compact：剥离 thinking
            if event.get("id") in compacted_ids:
                blocks = [b for b in blocks if b.get("type") != "thinking"]
            text_parts = [b["text"] for b in blocks if b.get("type") == "text"]
            thinking_parts = [b["thinking"] for b in blocks if b.get("type") == "thinking"]
            tool_calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {
                        "name": b["name"],
                        "arguments": _serialize_arguments(b.get("arguments", {})),
                    },
                }
                for b in blocks if b.get("type") == "toolCall"
            ]
            content = "\n".join(text_parts) if text_parts else None
            thinking = "\n".join(thinking_parts) if thinking_parts else None
            # 全空 → 跳过
            if not content and not tool_calls and not thinking:
                continue
            msg: dict = {"role": "assistant"}
            if content:
                msg["content"] = content
            if tool_calls:
                msg["tool_calls"] = tool_calls
            if thinking:
                msg["reasoning_content"] = thinking
            messages.append(msg)
            continue

        # ── 旧协议兼容：tool_result ──
        if _t == "tool_result":
            result_content = data.get("result", "")
            error = data.get("error")
            # fast compact：占位符
            if event.get("id") in compacted_ids:
                result_content = "[工具输出已压缩]"
            messages.append({
                "role": "tool",
                "tool_call_id": data.get("id", ""),
                "content": result_content,
            })
            continue

        # ── 新协议：AgentScope 事件 → 按 reply_id 分组 ──
        ev_type = event_row_type(event)
        if ev_type in _AGENTSCOPE_TYPES:
            # 跳过不产出消息的事件类型
            if ev_type in _SKIP_TYPES:
                continue
            reply_id = event.get("reply_id", "") or data.get("reply_id", "")
            if not reply_id:
                continue
            if reply_id not in reply_groups:
                reply_groups[reply_id] = []
                reply_order.append(reply_id)
            reply_groups[reply_id].append(idx)

    # ── 处理 AgentScope 事件分组 ──
    for reply_id in reply_order:
        group_indices = reply_groups[reply_id]
        group_events = [events[i] for i in group_indices]
        rebuilt = _rebuild_msg_from_events(group_events)
        messages.extend(rebuilt)

    return messages


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════

def _serialize_arguments(arguments: Any) -> str:
    """将 arguments 序列化为 JSON 字符串（OpenAI tool_calls 要求 string）。"""
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments, ensure_ascii=False)


def _safe_name(s: str) -> str:
    """把任意字符串规整为 OpenAI 允许的 name（^[a-zA-Z0-9_-]+$，长度<=64）。"""
    cleaned = "".join(c if (c.isalnum() or c in "_-") else "_" for c in s).strip("_")
    return (cleaned or "external")[:64]
