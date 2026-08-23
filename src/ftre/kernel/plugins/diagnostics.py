"""Lifecycle diagnostics exposed to startup and health tooling."""
# 中文说明：Plugin 诊断模型：记录状态、错误和依赖缺失，供启动日志、健康路由和测试使用。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cordis import FiberState


@dataclass(frozen=True)
class PluginStatus:
    """Stable, JSON-friendly view of one Plugin Fiber and its startup outcome."""
    id: str
    source: str
    entry: str
    state: FiberState | str
    required: bool
    error: str | None = None
    error_code: str | None = None
    missing: tuple[str, ...] = ()
    restart_required: bool = False
    duration_ms: float | None = None
    contributions: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Serialize Enum state and tuple fields for HTTP/CLI consumers."""
        return {
            "id": self.id,
            "source": self.source,
            "entry": self.entry,
            "state": self.state.value if isinstance(self.state, FiberState) else self.state,
            "required": self.required,
            "error": self.error,
            "error_code": self.error_code,
            "missing": list(self.missing),
            "restart_required": self.restart_required,
            "duration_ms": self.duration_ms,
            "contributions": [dict(item) for item in self.contributions],
        }


class PluginStartupError(RuntimeError):
    """Raised when one or more required Plugins cannot become ACTIVE."""
    def __init__(self, message: str, statuses: tuple[PluginStatus, ...] = ()) -> None:
        super().__init__(message)
        self.statuses = statuses
