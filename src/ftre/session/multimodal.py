"""
多模态用户消息构造

把 user_input 帧的 (content, attachments) 折成 OpenAI Chat Completions
所要求的 user message content：
- 无附件 → 直接返回 str（保持向后兼容、节省 token）
- 有附件 → 返回 list[part]，例如：
    [
      {"type": "text", "text": "看下这张图"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
    ]

注意：附件合法性已在 channel 入口（ws_channel._validate_attachments）校验过。
此处只做格式拼装，不再二次校验，避免重复解码 base64 浪费内存。
"""
from __future__ import annotations


IMAGE_OMITTED_NOTICE = "[图片附件已省略：当前模型不支持视觉输入]"


def build_user_content(
    text: str,
    attachments: list[dict] | None,
    *,
    include_images: bool = True,
) -> str | list[dict]:
    """
    根据文本 + 附件构造 OpenAI user message 的 content。

    Args:
        text:        文本主体，可空
        attachments: 形如 [{"type":"image","mime_type":"image/png","data":"<b64>"}]

    Returns:
        - 纯文本场景：str（兼容旧链路）
        - 含附件场景：list[dict]
    """
    if not attachments:
        return text or ""

    if not include_images:
        parts = [text] if text else []
        parts.append(IMAGE_OMITTED_NOTICE)
        return "\n\n".join(parts)

    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})

    for att in attachments:
        if att.get("type") == "image":
            mime = att.get("mime_type", "")
            b64 = att.get("data", "")
            if not mime or not b64:
                continue
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })

    # 极端兜底：附件全被跳过且无文本，给个空字符串避免 LLM 拒收
    if not parts:
        return text or ""
    return parts
