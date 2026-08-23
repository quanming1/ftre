"""ConfigService 专用的原子 JSON 存储。

这个类故意不承担配置 merge、revision 或 watcher 规则；它只负责安全读取和
``temp + fsync + replace`` 写入一个 JSON 对象，避免把文件系统细节扩散到
ConfigService 的业务代码。
"""

from __future__ import annotations

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
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("config root must be an object")
        return raw

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
