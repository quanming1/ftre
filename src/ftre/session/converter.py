"""消息格式转换层。

职责：直接把 ftre 的 events → 各 LLM provider 的 API 格式。
不加中间协议，不做持久化，不碰 DB。

    events → to_openai(events)       → OpenAI messages[]
    events → to_anthropic(events)    → Anthropic messages[]  （将来）

变换逻辑（compacted 占位符）是 provider 无关的，
提取为共享预处理函数，各 to_xxx() 复用。
"""

from __future__ import annotations

import json
from typing import Any

from ftre.session.manager import MessageModel
from ftre.session.multimodal import build_user_content, normalize_user_content


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

        # ── assistant_message_complete ──
        elif _t == "assistant_message_complete":
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

        # ── tool_result ──
        elif _t == "tool_result":
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

        # ── external_message ──
        elif _t == "external_message":
            from_ch = data.get("from_channel", "")
            from_sid = data.get("from_session", "")
            src = f"{from_ch}::{from_sid}" if from_ch or from_sid else "external"
            messages.append({
                "role": "assistant",
                "name": _safe_name(src),
                "content": f"[来自 {src} 的消息] {data.get('content', '')}",
            })

        # ── context_compact ──
        elif _t == "context_compact":
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
