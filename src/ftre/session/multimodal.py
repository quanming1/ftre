"""
Build OpenAI-safe user message content from FTRE user message parts.

Internal UI/user parts may contain FTRE-only types such as ``skill``. This
module is the boundary that converts them into provider-safe content parts.
"""
from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape

IMAGE_OMITTED_NOTICE = "[图片附件已省略：当前模型不支持视觉输入]"


def _text_value(part: dict[str, Any]) -> str:
    """Read the unified text field, with legacy data fallback."""
    return str(part.get("text") or part.get("data") or "").strip()


def skill_part_to_text(name: str) -> str:
    """Convert a selected skill reference into an instruction text part."""
    safe_name = escape(name.strip())
    if not safe_name:
        return ""
    return (
        f'<selected_skill name="{safe_name}">\n'
        "请调用 loadSkill 加载此 Skill 的完整内容。\n"
        "</selected_skill>"
    )


def _content_to_text(content: Any) -> str:
    """Normalize string or structured parts into plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = _text_value(part)
            if text:
                parts.append(text)
        elif ptype == "skill":
            text = skill_part_to_text(str(part.get("data") or part.get("text") or ""))
            if text:
                parts.append(text)
    return "\n".join(parts)


def normalize_stored_user_content(content: Any) -> list[dict[str, Any]]:
    """Normalize user message content for DB/UI storage."""
    if isinstance(content, str):
        text = content.strip()
        return [{"type": "text", "text": text}] if text else []
    if not isinstance(content, list):
        return []

    normalized: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = _text_value(part)
            if text:
                normalized.append({"type": "text", "text": text})
        elif ptype == "skill":
            name = str(part.get("data") or part.get("text") or "").strip()
            if name:
                normalized.append({"type": "skill", "data": name})
        elif ptype == "image":
            data = part.get("data")
            if isinstance(data, dict):
                normalized.append({"type": "image", "data": data})
    return normalized


def normalize_user_content(
    content: Any,
    *,
    include_images: bool = True,
) -> str | list[dict[str, Any]]:
    """
    Convert FTRE user content to OpenAI-safe content.

    The output only contains provider-supported part types:
    - text
    - image_url, when include_images is true

    FTRE-only parts such as skill are converted to text. Unknown UI-only parts
    are intentionally dropped so they cannot leak into provider requests.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    normalized: list[dict[str, Any]] = []
    text_only: list[str] = []
    omitted_image = False

    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")

        if ptype == "text":
            text = _text_value(part)
            if text:
                normalized.append({"type": "text", "text": text})
                text_only.append(text)
        elif ptype == "skill":
            text = skill_part_to_text(str(part.get("data") or part.get("text") or ""))
            if text:
                normalized.append({"type": "text", "text": text})
                text_only.append(text)
        elif ptype == "image_url":
            if include_images:
                image_url = part.get("image_url")
                if isinstance(image_url, dict) and image_url.get("url"):
                    normalized.append({"type": "image_url", "image_url": image_url})
            else:
                omitted_image = True
        elif ptype == "image":
            omitted_image = True

    if omitted_image:
        text_only.append(IMAGE_OMITTED_NOTICE)
        if include_images:
            # UI image parts are render-only. Real image payloads arrive through
            # attachments and are handled by build_user_content.
            pass
        else:
            normalized.append({"type": "text", "text": IMAGE_OMITTED_NOTICE})

    if not include_images:
        return "\n\n".join(text_only)

    if not normalized:
        return ""

    only_text = all(part.get("type") == "text" for part in normalized)
    if only_text:
        return "\n\n".join(str(part.get("text") or "") for part in normalized)
    return normalized


def build_user_content(
    content: Any,
    attachments: list[dict[str, Any]] | None,
    *,
    include_images: bool = True,
) -> str | list[dict[str, Any]]:
    """
    Build OpenAI user message content from text/parts plus optional attachments.
    """
    if not attachments:
        return normalize_user_content(content, include_images=include_images)

    text = _content_to_text(content)

    if not include_images:
        lines = [text] if text else []
        lines.append(IMAGE_OMITTED_NOTICE)
        return "\n\n".join(lines)

    parts_multi: list[dict[str, Any]] = []
    if text:
        parts_multi.append({"type": "text", "text": text})

    for att in attachments:
        if att.get("type") != "image":
            continue
        mime = att.get("mime_type", "")
        b64 = att.get("data", "")
        if not mime or not b64:
            continue
        parts_multi.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        )

    if not parts_multi:
        return text or ""
    return parts_multi
