"""
Token 估算（字符级粗估，不依赖 tiktoken）

用于"还没经过 LLM 实算"那部分事件的预估，配合最近一次真实 usage_update
拼出 session 当前总用量。

策略：先复用 SessionManager.to_openai_messages 把事件流折成 OpenAI 协议
消息（单一信任源；tool_call 合并 / 多模态等规则不再手写），再对每条
message 做字符级估算。

规则：
- CJK 字符: 1 字 ≈ 1 token
- 其它字符: 4 字符 ≈ 1 token（向上取整）
- 每条 message: +4 元开销（role / 分隔符等）
- image_url part: 固定 85 token（OpenAI low-detail 兜底）
"""
from __future__ import annotations

import json
from typing import Any

MESSAGE_OVERHEAD = 4
IMAGE_TOKEN_FALLBACK = 85


def _is_cjk(ch: str) -> bool:
    return (
        "\u4e00" <= ch <= "\u9fff"
        or "\u3040" <= ch <= "\u30ff"
        or "\uac00" <= ch <= "\ud7af"
        or "\u3400" <= ch <= "\u4dbf"
    )


def estimate_text_tokens(text: str | None) -> int:
    """字符级粗估：CJK 1 字 = 1 token，其它 4 字符 = 1 token（向上取整）"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def _estimate_message(message: dict[str, Any]) -> int:
    """单条 OpenAI message 的 token（含 content / name / tool_calls）"""
    tokens = MESSAGE_OVERHEAD

    if name := message.get("name"):
        tokens += estimate_text_tokens(name)

    content = message.get("content")
    if isinstance(content, str):
        tokens += estimate_text_tokens(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                tokens += estimate_text_tokens(part.get("text", ""))
            elif part.get("type") == "image_url":
                # 不能对 base64 data URL 做字符估算
                tokens += IMAGE_TOKEN_FALLBACK

    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        tokens += estimate_text_tokens(fn.get("name", ""))
        args = fn.get("arguments", "")
        if not isinstance(args, str):
            try:
                args = json.dumps(args, ensure_ascii=False)
            except Exception:
                args = str(args)
        tokens += estimate_text_tokens(args)

    return tokens


def estimate_events_tokens(events: list[dict[str, Any]]) -> int:
    """估算一批事件折成 OpenAI messages 后的总 token"""
    if not events:
        return 0
    # 延迟 import，避免与 manager 循环
    from .manager import SessionManager

    return sum(_estimate_message(m) for m in SessionManager.to_openai_messages(events))
