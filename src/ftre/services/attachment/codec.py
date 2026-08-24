"""Pure image encoding helpers used at the message/provider boundary."""
# 中文说明：附件编码边界：把本地图片转成安全的 data URL/消息附件形状，纯函数不保存文件。

from __future__ import annotations

import base64
import logging
import os

logger = logging.getLogger(__name__)

_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def load_as_data_url(path: str, mime: str = "") -> str | None:
    """Read an image path as a provider-safe data URL.

    This is intentionally a pure boundary helper: persistence and path
    ownership remain in ``AttachmentService``; message normalization only
    needs a bytes-to-wire conversion and must not create a Service.
    """
    try:
        with open(path, "rb") as stream:
            raw = stream.read()
        if not mime:
            mime = _EXT_TO_MIME.get(os.path.splitext(path)[1].lower(), "image/png")
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    except Exception:
        logger.warning("[attachment-codec] failed to load %s", path, exc_info=True)
        return None


__all__ = ["load_as_data_url"]
