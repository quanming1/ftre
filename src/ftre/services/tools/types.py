from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolContribution:
    name: str
    owner: str
    source: str
    scope: str
    tool: Any

