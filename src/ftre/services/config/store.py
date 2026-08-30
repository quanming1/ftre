"""ConfigService 专用的原子 JSON 存储。

这个类故意不承担配置 merge、revision 或 watcher 规则；它只负责安全读取和
``temp + fsync + replace`` 写入一个 JSON 对象，避免把文件系统细节扩散到
ConfigService 的业务代码。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class JsonConfigStore:
    """配置文件的最小读写适配器，不保存长期内存状态。"""
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, Any]:
        value, _content_hash = self.read_with_hash()
        return value

    def read_with_hash(self) -> tuple[dict[str, Any], str]:
        """读取同一份字节并返回 JSON 对象和内容指纹。"""
        if not self.path.exists():
            return {}, ""
        raw_bytes = self.path.read_bytes()
        raw = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("config root must be an object")
        return raw, hashlib.sha256(raw_bytes).hexdigest()

    def signature(self) -> tuple[int, int, int] | None:
        """返回轻量文件指纹；文件不存在时返回 None。"""
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size

    def write_atomic(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
