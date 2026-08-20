from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class PromptSection:
    name: str
    content: str | None = None
    factory: Callable[[dict[str, Any]], str] | None = None
    priority: int = 100
    scope: str = "global"
    required: bool = False
    owner: str = "system"
    source: str = "builtin"

