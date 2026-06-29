"""
统一图片暂存工具。

用户上传附件和 read 工具读图都通过本模块将图片数据落盘到 OS temp 目录，
事件链路中只携带文件路径；base64 转换在 SessionManager 出口完成。
"""
from __future__ import annotations

import base64
import logging
import os
import re
import uuid

logger = logging.getLogger(__name__)

_IMG_DIR = os.path.join(os.path.expanduser("~"), ".ftre", "assets", "images")

_MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

_EXT_TO_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _sanitize_filename(name: str) -> str:
    """保留原始文件名，仅保留 [a-zA-Z0-9._-] 字符，其余替换为 _。"""
    name = os.path.basename(name)
    name = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    if not name or name in (".", ".."):
        name = f"image_{uuid.uuid4().hex[:8]}"
    return name


def save_image(raw: bytes, mime: str, original_name: str = "") -> str:
    """将 raw bytes 存到 temp 目录，返回绝对路径。

    Args:
        raw: 图片二进制数据
        mime: MIME 类型，如 "image/png"
        original_name: 原始文件名，用于命名（会做安全过滤）

    Returns:
        存储文件的绝对路径
    """
    os.makedirs(_IMG_DIR, exist_ok=True)

    ext = _MIME_TO_EXT.get(mime, ".png")

    if original_name:
        name = _sanitize_filename(original_name)
        # 确保扩展名正确
        if not name.lower().endswith(ext):
            name = f"{name}{ext}"
    else:
        name = f"image_{uuid.uuid4().hex[:8]}{ext}"

    path = os.path.join(_IMG_DIR, name)

    # 文件名冲突时追加 _1, _2, ...
    if os.path.exists(path):
        base_part, ext_part = os.path.splitext(name)
        counter = 1
        while os.path.exists(path):
            path = os.path.join(_IMG_DIR, f"{base_part}_{counter}{ext_part}")
            counter += 1

    with open(path, "wb") as f:
        f.write(raw)

    logger.debug(f"[image_store] saved {len(raw)} bytes -> {path}")
    return path


def load_as_data_url(path: str, mime: str = "") -> str | None:
    """从路径读取文件，返回 data URL。失败返回 None。

    Args:
        path: 文件绝对路径
        mime: MIME 类型。为空时从文件扩展名推断。

    Returns:
        形如 "data:image/png;base64,xxxx" 的 data URL，或 None
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()

        if not mime:
            ext = os.path.splitext(path)[1].lower()
            mime = _EXT_TO_MIME.get(ext, "image/png")

        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        logger.warning(f"[image_store] failed to load {path}: {e}")
        return None
