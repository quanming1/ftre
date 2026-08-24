"""文件系统路径策略模型。

PathPolicy 只做边界判断，不执行读写；LocalFilesystemService 负责把它应用到
具体 IO。把两者拆开便于 Tools/Workspace 共享同一安全规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class PathViolation(PermissionError):
    """目标路径不在允许 root/目录内。"""
    code = "filesystem_path_violation"


@dataclass(frozen=True)
class PathPolicy:
    """限制路径只能落在 root 或额外 allow-list 目录下。"""
    root: Path | None = None
    allowed_dirs: tuple[Path, ...] = field(default_factory=tuple)
    allow_missing: bool = True

    def check(self, path: Path) -> Path:
        candidate = path.expanduser().resolve(strict=False)
        roots = tuple(root.expanduser().resolve(strict=False) for root in self.allowed_dirs)
        if self.root is not None:
            roots = (self.root.expanduser().resolve(strict=False),) + roots
        if roots and not any(candidate == root or root in candidate.parents for root in roots):
            raise PathViolation(f"path escapes policy: {candidate}")
        return candidate
