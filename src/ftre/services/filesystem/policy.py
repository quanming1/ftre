from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class PathViolation(PermissionError):
    code = "filesystem_path_violation"


@dataclass(frozen=True)
class PathPolicy:
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

