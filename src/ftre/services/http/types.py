"""HTTP Service 使用的路由贡献数据模型。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RouteContribution:
    """一个 Plugin 对 HTTP Host 的不可变路由声明。"""
    method: str
    path: str
    owner: str
    kind: str = "exact"
    router: Any | None = None
    handler: Callable[..., Any] | None = None
