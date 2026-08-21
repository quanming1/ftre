"""Composition-facing plugin manager."""

from __future__ import annotations

from typing import Any

from cordis import Context

from .catalog import PluginCatalog
from .diagnostics import PluginStatus
from .loader import PluginLoader
from .manifest import PluginManifest


class PluginManager:
    """Composition-facing facade that applies config selection over PluginLoader."""
    def __init__(self, context: Context, *, plugins_dir=None) -> None:
        from .discovery import PluginDiscovery

        self.context = context
        self.loader = PluginLoader(context, discovery=PluginDiscovery(plugins_dir=plugins_dir))
        self.catalog: PluginCatalog | None = None
        self._statuses: tuple[PluginStatus, ...] = ()

    async def load(
        self,
        builtins: list[PluginManifest],
        config: dict[str, Any] | None = None,
    ) -> tuple[PluginStatus, ...]:
        """Build the catalog, filter disabled/external entries, then load selected Fibers."""
        self.catalog = self.loader.discovery.catalog(builtins, config)
        entries = config.get("plugins", []) if isinstance(config, dict) else []
        overrides = {
            str(item.get("id") or item.get("name")): item
            for item in entries
            if isinstance(item, dict)
        }
        selected: list[PluginManifest] = []
        for manifest in self.catalog.values():
            override = overrides.get(manifest.id, {})
            disabled = bool(override.get("disabled", override.get("enabled") is False))
            if manifest.source.startswith("external:") and not override:
                continue
            if disabled:
                continue
            required = manifest.required or bool(override.get("required", False))
            selected.append(
                PluginManifest(
                    id=manifest.id,
                    entry=manifest.entry,
                    source=manifest.source,
                    required=required,
                    default_enabled=manifest.default_enabled,
                    version=manifest.version,
                    description=manifest.description,
                    config={**manifest.config, **dict(override.get("config") or {})},
                )
            )
        self._statuses = await self.loader.load(selected)
        return self._statuses

    def statuses(self) -> tuple[PluginStatus, ...]:
        """Return current Fiber states suitable for startup and health output."""
        return self.loader.statuses() or self._statuses

    def diagnostics(self) -> list[dict[str, Any]]:
        """Return status dictionaries for JSON responses and logs."""
        return [status.as_dict() for status in self.statuses()]

    async def unload(self, plugin_id: str) -> bool:
        """Delegate reversible unload while retaining a stable public API."""
        return await self.loader.unload(plugin_id)

    async def close(self) -> None:
        """Close the Context once; Cordis makes repeated cleanup safe."""
        await self.loader.dispose()
