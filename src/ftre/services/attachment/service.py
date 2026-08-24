"""请求附件 Service：在一个根目录内保存、读取和识别上传文件。

附件是 Channel/API 边界的数据，不属于 Session 历史或 Tool 工作区。Service
通过文件名净化和 root 校验阻断路径穿越；调用方拿到的是文件内容/元数据，而不
需要自己拼接用户目录。
"""

from __future__ import annotations

import mimetypes
import os
import re
import uuid
from pathlib import Path
from typing import Any


class AttachmentService:
    """在配置根目录内解析和读写附件，阻止路径穿越。"""
    key = "attachments"

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or (Path.home() / ".ftre" / "assets" / "images")).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, filename: str) -> Path:
        """把单一文件名解析到 root 内，拒绝目录和路径穿越。"""
        safe = os.path.basename(filename)
        if safe != filename:
            raise ValueError("非法文件名")
        path = (self.root / safe).resolve()
        if path.parent != self.root:
            raise ValueError("attachment path escapes root")
        return path

    def stat(self, filename: str) -> dict[str, Any]:
        """返回附件大小和 MIME，不把 Path 对象泄露给协议层。"""
        path = self.resolve(filename)
        info = path.stat()
        return {"filename": filename, "size": info.st_size, "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream"}

    def read(self, filename: str, limit: int = 10_000_000) -> bytes:
        """按上限读取附件字节，避免异常大文件一次性进入内存。"""
        return self.resolve(filename).read_bytes()[:limit]

    def save_image(self, raw: bytes, mime: str, original_name: str = "") -> str:
        """Persist image bytes under this Service's configured root."""
        if not isinstance(raw, bytes):
            raise TypeError("attachment data must be bytes")
        extension = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(mime, ".bin")
        name = os.path.basename(original_name or f"attachment_{uuid.uuid4().hex[:8]}")
        name = re.sub(r"[^a-zA-Z0-9._-]", "_", name) or f"attachment_{uuid.uuid4().hex[:8]}"
        if not name.lower().endswith(extension):
            name = f"{name}{extension}"
        candidate = self.resolve(name)
        counter = 1
        while candidate.exists():
            stem = candidate.stem
            candidate = self.resolve(f"{stem}_{counter}{candidate.suffix}")
            counter += 1
        candidate.write_bytes(raw)
        return str(candidate)

    def resolve_local_image(self, path: str) -> tuple[Path, str]:
        """Resolve a renderer preview path while preserving the old image contract."""
        candidate = Path(os.path.abspath(os.path.expanduser(path)))
        if not candidate.is_file():
            raise FileNotFoundError(path)
        mime = mimetypes.guess_type(candidate.name)[0]
        if not mime or not mime.startswith("image/"):
            raise ValueError("not an image file")
        return candidate, mime
