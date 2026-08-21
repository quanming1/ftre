"""Serializable startup snapshot for embedded hosts and health diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StartupDiagnostics:
    """Describe composition phase, contributed routes, and listener state."""
    phase: str
    plugins: tuple[dict[str, Any], ...]
    routes: tuple[dict[str, Any], ...]
    listening: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Convert the immutable snapshot into the shape used by diagnostics APIs."""
        return {"phase": self.phase, "plugins": [dict(item) for item in self.plugins], "routes": [dict(item) for item in self.routes], "listening": self.listening}
