"""
see_img 工具 — 让 Agent 查看图片。

支持：本地绝对路径 | HTTP(S) URL
大图自动压缩（>5MB 或 >4096px resize）
非图片文件 → 返回文本内容
"""
from __future__ import annotations

import base64
import io
import logging
import os
import urllib.request
from pathlib import Path

from ftre_agent_core.agent.event import UserMessageEvent, user_message_event
from ftre_agent_core.tool import Tool, ToolParameter

logger = logging.getLogger(__name__)

# ─── 压缩阈值 ──────────────────────────────────────────
MAX_FILE_SIZE = 5 * 1024 * 1024   # 5MB
MAX_DIMENSION = 4096              # 像素


def _load_image(src: str) -> tuple[bytes, str]:
    """读取图片并返回 (bytes, mime_type)。"""
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "ftre-see-img"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        ct = resp.headers.get("Content-Type", "image/png")
        return data, ct

    path = Path(src)
    if not path.is_absolute():
        raise ValueError(f"see_img 需要绝对路径: {src}")
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {src}")

    ext = path.suffix.lower()
    mime_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
    }
    mime = mime_map.get(ext, "image/png")
    return path.read_bytes(), mime


def _compress(data: bytes, mime: str) -> tuple[bytes, str]:
    """图片太大时压缩：先按尺寸缩放，再按质量压缩。"""
    if len(data) <= MAX_FILE_SIZE:
        return data, mime

    from PIL import Image   # lazy import

    img = Image.open(io.BytesIO(data))
    w, h = img.size

    # 尺寸缩放
    if max(w, h) > MAX_DIMENSION:
        ratio = MAX_DIMENSION / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

    fmt = img.format or "JPEG"
    buf = io.BytesIO()
    if fmt in ("JPEG", "WEBP"):
        img.save(buf, format=fmt, quality=70, optimize=True)
    else:
        img.save(buf, format=fmt, optimize=True)

    data2 = buf.getvalue()
    if len(data2) > MAX_FILE_SIZE:
        # 二次压缩：缩小到 2048
        w, h = img.size
        ratio = min(2048 / max(w, h), 1.0)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60, optimize=True)
        data2 = buf.getvalue()

    mime2 = f"image/{fmt.lower()}"
    return data2, mime2


def _to_base64(data: bytes, mime: str) -> str:
    """转为 data URI。"""
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def see_img(path: str) -> str | UserMessageEvent:
    """查看图片，返回 base64 编码的多模态 user message。

    参数:
        path: 本地绝对路径 或 HTTP(S) URL

    返回:
        图片文件 → UserMessageEvent（LLM 可见，前端隐藏）
        非图片文件 → str（文件文本内容）
    """
    try:
        data, mime = _load_image(path)
    except Exception as e:
        return f"[see_img] 读取失败: {e}"

    # 检测是否图片
    if not mime.startswith("image/"):
        try:
            return data.decode("utf-8")[:10000]
        except UnicodeDecodeError:
            return f"[see_img] 非图片文件: {path} ({mime}, {len(data)} bytes)"

    # 压缩
    data, mime = _compress(data, mime)
    data_uri = _to_base64(data, mime)

    logger.info(f"[see_img] {path} → {mime}, {len(data)} bytes")

    return user_message_event(
        content=[{
            "type": "image_url",
            "image_url": {"url": data_uri, "detail": "auto"},
        }],
        metadata={"hide": True, "path": path, "mime": mime, "size": len(data)},
    )


def create_see_img_tool() -> Tool:
    """创建 see_img 工具。"""
    return Tool(
        name="see_img",
        description=(
            "查看图片，返回 LLM 可识别的图片内容。大图自动压缩至 5MB/4096px 以内。"
            "支持本地绝对路径（C:/photo.png）和 HTTP 地址（https://...）。"
            "非图片文件返回文本内容。"
        ),
        parameters=[
            ToolParameter(
                name="path",
                type="string",
                description="图片路径（本地绝对路径如 C:/photo.png 或 HTTP URL）",
                required=True,
            ),
        ],
        func=see_img,
    )
