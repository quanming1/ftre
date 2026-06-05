"""
多模态用户消息构造

把 user_input 帧的 (content, attachments) 折成 OpenAI Chat Completions
所要求的 user message content：

content 支持两种形态（向下兼容）：
- 字符串：纯文本（旧协议），直接使用
- 对象数组：结构化 parts，每项 {type, data}：
    {"type": "text", "data": "..."}
    {"type": "skill", "data": "<skill-name>"}

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


def _content_to_text(content: str | list[dict]) -> str:
    """将 content（string 或 parts 数组）归一化为纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        t = part.get("type", "")
        if t == "text":
            d = (part.get("data") or "").strip()
            if d:
                parts.append(d)
        elif t == "skill":
            name = part.get("data", "")
            if name:
                parts.append(f"[已选择 Skill: {name}]")
    return "\n".join(parts)


def build_user_content(
    content: str | list[dict],
    attachments: list[dict] | None,
    *,
    include_images: bool = True,
) -> str | list[dict]:
    """
    根据文本/结构化内容 + 附件构造 OpenAI user message 的 content。

    Args:
        content:     文本主体或结构化 parts 数组，可空
        attachments: 形如 [{"type":"image","mime_type":"image/png","data":"<b64>"}]

    Returns:
        - 纯文本场景：str（兼容旧链路）
        - 含附件场景：list[dict]
    """
    text = _content_to_text(content)

    if not attachments:
        return text or ""

    if not include_images:
        lines = [text] if text else []
        lines.append(IMAGE_OMITTED_NOTICE)
        return "\n\n".join(lines)

    parts_multi: list[dict] = []
    if text:
        parts_multi.append({"type": "text", "text": text})

    for att in attachments:
        if att.get("type") == "image":
            mime = att.get("mime_type", "")
            b64 = att.get("data", "")
            if not mime or not b64:
                continue
            parts_multi.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })

    # 极端兜底：附件全被跳过且无文本，给个空字符串避免 LLM 拒收
    if not parts_multi:
        return text or ""
    return parts_multi
