"""Provider Plugin for the user configuration Service."""

from __future__ import annotations

from typing import Any

from cordis import Context

from .service import ConfigService

inject = ()
provide = ("config",)


async def apply(ctx: Context, config: dict[str, Any] | None = None):
    """Create the config owner; Composition-injected instances take precedence."""
    existing = ctx.get("config", strict=False)
    if existing is not None:
        return
    service = ConfigService(initial=config if isinstance(config, dict) and config.get("initial") else None)
    ctx.provide("config", service)
