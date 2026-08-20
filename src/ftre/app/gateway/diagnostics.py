from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StartupDiagnostics:
    phase: str
    plugins: tuple[dict[str, Any], ...]
    routes: tuple[dict[str, Any], ...]
    listening: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"phase": self.phase, "plugins": [dict(item) for item in self.plugins], "routes": [dict(item) for item in self.routes], "listening": self.listening}

