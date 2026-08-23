"""经过路径策略解析后的文件目标值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileTarget:
    """不可变的安全路径句柄；不携带打开的文件描述符。"""
    path: Path
    policy: str = "default"
