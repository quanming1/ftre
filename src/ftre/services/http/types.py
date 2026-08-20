from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RouteContribution:
    method: str
    path: str
    owner: str
    kind: str = "exact"
    router: Any | None = None
    handler: Callable[..., Any] | None = None

