"""Manifest catalog with explicit duplicate detection."""

from __future__ import annotations

from collections.abc import Iterable

from .manifest import PluginManifest


class PluginCatalog:
    """Immutable-by-snapshot manifest index that rejects duplicate IDs early."""
    def __init__(self, manifests: Iterable[PluginManifest] = ()) -> None:
        self._items: dict[str, PluginManifest] = {}
        for manifest in manifests:
            self.add(manifest)

    def add(self, manifest: PluginManifest) -> None:
        """Add one candidate; duplicate IDs are configuration errors, not overrides."""
        previous = self._items.get(manifest.id)
        if previous is not None:
            raise ValueError(
                f"plugin id conflict: {manifest.id!r} ({previous.source} vs {manifest.source})"
            )
        self._items[manifest.id] = manifest

    def get(self, plugin_id: str) -> PluginManifest | None:
        """Return a candidate by stable ID, or ``None`` when it is absent."""
        return self._items.get(plugin_id)

    def require(self, plugin_id: str) -> PluginManifest:
        """Return a candidate or raise a clear missing-plugin error."""
        manifest = self.get(plugin_id)
        if manifest is None:
            raise KeyError(plugin_id)
        return manifest

    def values(self) -> tuple[PluginManifest, ...]:
        """Expose candidates in deterministic insertion order."""
        return tuple(self._items.values())

    def snapshot(self) -> tuple[PluginManifest, ...]:
        """Return an immutable catalog snapshot for diagnostics and tests."""
        return tuple(self._items.values())

    def __contains__(self, plugin_id: str) -> bool:
        return plugin_id in self._items
