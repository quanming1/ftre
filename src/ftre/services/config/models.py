from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConfigValue:
    revision: int
    value: dict[str, Any]

