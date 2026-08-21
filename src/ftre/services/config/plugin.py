"""Provider Plugin for the user configuration Service."""

from __future__ import annotations

from typing import Any

from cordis import PluginContext

from .service import ConfigService

inject = ()
provide = ("config",)


async def apply(ctx: PluginContext, config: dict[str, Any] | None = None):
    """Create the config owner; Composition-injected instances take precedence."""
    existing = ctx.optional("config")
    if existing is not None:
        return
    service = ConfigService(initial=config if isinstance(config, dict) and config.get("initial") else None)
    ctx.provide("config", service)
