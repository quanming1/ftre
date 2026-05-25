"""
Token 估算（字符级粗估，不依赖 tiktoken）

仅用于"还没经过 LLM 实算"那部分事件的 token 预估，配合最近一次
真实 usage_update 拼出 session 当前总用量。

规则（粗略对齐 OpenAI 经验值）：
- CJK 字符（中日韩）: 1 字 ≈ 1 token
- 其它字符：每 4 个字符 ≈ 1 token（向上取整）
- 每条 message 加 +4 元开销（role / 分隔符等）
- 图片附件：每张固定 85 token（OpenAI low-detail 兜底）

不追求精确，只用来给前端一个"上下文水位"参考。
"""
from __future__ import annotations

import json
from typing import Any

# 每条 message 的元开销（role / 分隔符等）
MESSAGE_OVERHEAD = 4

# 图片附件兜底 token（OpenAI low-detail 默认）
IMAGE_TOKEN_FALLBACK = 85


def _is_cjk(ch: str) -> bool:
    """判断单字是否属于中日韩范围"""
    return (
        "\u4e00" <= ch <= "\u9fff"   # CJK Unified Ideographs
        or "\u3040" <= ch <= "\u30ff"  # 日文 平/片假名
        or "\uac00" <= ch <= "\ud7af"  # 韩文 谚文
        or "\u3400" <= ch <= "\u4dbf"  # CJK Extension A
    )


def estimate_text_tokens(text: str | None) -> int:
    """
    估算一段文本的 token 数。
    - CJK 字符：1 字 = 1 token
    - 其它字符：累加后按 4 字符 ≈ 1 token 向上取整
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            other += 1
    # 其它字符向上取整到 4 字符 = 1 token
    other_tokens = (other + 3) // 4
    return cjk + other_tokens


def estimate_attachment_tokens(att: dict[str, Any]) -> int:
    """估算单个附件的 token；目前只识别 image 类型"""
    if not isinstance(att, dict):
        return 0
    if att.get("type") == "image":
        return IMAGE_TOKEN_FALLBACK
    return 0


def estimate_event_tokens(event: dict[str, Any]) -> int:
    """
    根据事件类型估算其折成 OpenAI message 后的 token 数。
    只对"会被 to_openai_messages 回放"的事件类型返回非零值，
    其余生命周期/控制类事件返回 0。
    """
    t = event.get("type", "")
    data = event.get("data") or {}

    if t == "USER_INPUT":
        text = data.get("content", "") or ""
        attachments = data.get("attachments") or []
        token = estimate_text_tokens(text)
        for att in attachments:
            token += estimate_attachment_tokens(att)
        return token + MESSAGE_OVERHEAD

    if t == "external_message":
        from_ch = data.get("from_channel", "") or ""
        from_sid = data.get("from_session", "") or ""
        src = f"{from_ch}::{from_sid}" if from_ch or from_sid else "external"
        body = f"[来自 {src} 的消息] {data.get('content', '') or ''}"
        return estimate_text_tokens(body) + MESSAGE_OVERHEAD

    if t == "tool_call":
        name = data.get("name", "") or ""
        args = data.get("arguments", {})
        if isinstance(args, str):
            args_str = args
        else:
            try:
                args_str = json.dumps(args, ensure_ascii=False)
            except Exception:
                args_str = str(args)
        return estimate_text_tokens(name) + estimate_text_tokens(args_str) + MESSAGE_OVERHEAD

    if t == "tool_result":
        result = data.get("result", "") or ""
        if not isinstance(result, str):
            result = str(result)
        return estimate_text_tokens(result) + MESSAGE_OVERHEAD

    if t == "message_complete":
        return estimate_text_tokens(data.get("content", "") or "") + MESSAGE_OVERHEAD

    # tool_cancel_requested / tool_cancelled / tool_timed_out / error / done
    # / usage_update 等不会进下次 prompt，返回 0
    return 0
