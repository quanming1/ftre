"""Local filesystem provider with one centralized path policy."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .policy import PathPolicy
from .target import FileTarget


class LocalFilesystemService:
    key = "filesystem"

    def resolve(self, path: str | Path, cwd: str | Path | None = None, policy: PathPolicy | None = None) -> FileTarget:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(cwd or os.getcwd()) / candidate
        checked = (policy or PathPolicy()).check(candidate)
        return FileTarget(checked)

    def stat(self, target: FileTarget | str | Path) -> dict[str, Any]:
        path = self._path(target)
        info = path.stat()
        return {"path": str(path), "kind": "directory" if path.is_dir() else "file", "size": info.st_size, "mtime": info.st_mtime}

    def read_text(self, target: FileTarget | str | Path, limit: int = 1_000_000, encoding: str = "utf-8") -> str:
        data = self._path(target).read_bytes()[:limit]
        return data.decode(encoding, errors="replace")

    def read_bytes(self, target: FileTarget | str | Path, limit: int = 10_000_000) -> bytes:
        with self._path(target).open("rb") as handle:
            return handle.read(limit)

    def write_text_atomic(self, target: FileTarget | str | Path, content: str, encoding: str = "utf-8") -> None:
        path = self._path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(content, encoding=encoding)
        os.replace(temp, path)

    def mkdir(self, target: FileTarget | str | Path, parents: bool = False) -> None:
        self._path(target).mkdir(parents=parents, exist_ok=True)

    @staticmethod
    def _path(target: FileTarget | str | Path) -> Path:
        return target.path if isinstance(target, FileTarget) else Path(target).expanduser().resolve(strict=False)

