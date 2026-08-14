"""Core service installation helpers for the plugin context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .context import FtreContext


@dataclass
class BaseService:
    """Named service wrapper for extensions that prefer an explicit service object."""

    name: str
    value: Any


def install_core_services(context: FtreContext, **services: Any) -> None:
    """Install non-None gateway services into the root plugin context."""
    for name, value in services.items():
        if value is not None:
            context.provide(name, value)
