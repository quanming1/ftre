"""Small root-confined store for files attached to API messages."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any


class AttachmentService:
    """Resolve and read attachments while preventing traversal outside the root."""
    key = "attachments"

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or (Path.home() / ".ftre" / "assets" / "images")).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, filename: str) -> Path:
        safe = os.path.basename(filename)
        if safe != filename:
            raise ValueError("非法文件名")
        path = (self.root / safe).resolve()
        if path.parent != self.root:
            raise ValueError("attachment path escapes root")
        return path

    def stat(self, filename: str) -> dict[str, Any]:
        path = self.resolve(filename)
        info = path.stat()
        return {"filename": filename, "size": info.st_size, "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream"}

    def read(self, filename: str, limit: int = 10_000_000) -> bytes:
        return self.resolve(filename).read_bytes()[:limit]
