from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileTarget:
    path: Path
    policy: str = "default"

