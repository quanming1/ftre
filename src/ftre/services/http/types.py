from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class RouteContribution:
    method: str
    path: str
    owner: str
    kind: str = "exact"
    router: Any | None = None
    handler: Callable[..., Any] | None = None

