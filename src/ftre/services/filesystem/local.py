"""本地文件系统 Service：把路径策略集中到一次解析后再执行 IO。

所有工具和 Feature 都应通过这个门面或更高层的 WorkspaceService 访问文件；
Service 本身不决定用户工作区，只负责把调用方给出的路径变成经过
``PathPolicy`` 校验的 ``FileTarget``。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .policy import PathPolicy
from .target import FileTarget


class LocalFilesystemService:
    """供 Tools/Feature 共享的、带路径策略的文件系统门面。"""
    key = "filesystem"

    def resolve(self, path: str | Path, cwd: str | Path | None = None, policy: PathPolicy | None = None) -> FileTarget:
        """Normalize a path and apply the caller's workspace policy before IO."""
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(cwd or os.getcwd()) / candidate
        checked = (policy or PathPolicy()).check(candidate)
        return FileTarget(checked)

    def stat(self, target: FileTarget | str | Path) -> dict[str, Any]:
        """读取目标的类型、大小和修改时间。"""
        path = self._path(target)
        info = path.stat()
        return {"path": str(path), "kind": "directory" if path.is_dir() else "file", "size": info.st_size, "mtime": info.st_mtime}

    def read_text(self, target: FileTarget | str | Path, limit: int = 1_000_000, encoding: str = "utf-8") -> str:
        """按字节上限读取文本，并用 replace 处理非法编码。"""
        data = self._path(target).read_bytes()[:limit]
        return data.decode(encoding, errors="replace")

    def read_bytes(self, target: FileTarget | str | Path, limit: int = 10_000_000) -> bytes:
        """按上限读取二进制内容。"""
        with self._path(target).open("rb") as handle:
            return handle.read(limit)

    def write_text_atomic(self, target: FileTarget | str | Path, content: str, encoding: str = "utf-8") -> None:
        """以同目录临时文件替换方式写入文本，避免半写文件。"""
        path = self._path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(content, encoding=encoding)
        os.replace(temp, path)

    def mkdir(self, target: FileTarget | str | Path, parents: bool = False) -> None:
        """创建目标目录；是否递归创建由调用方显式决定。"""
        self._path(target).mkdir(parents=parents, exist_ok=True)

    @staticmethod
    def _path(target: FileTarget | str | Path) -> Path:
        return target.path if isinstance(target, FileTarget) else Path(target).expanduser().resolve(strict=False)
