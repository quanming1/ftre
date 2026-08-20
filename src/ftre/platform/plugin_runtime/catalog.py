"""Manifest catalog with explicit duplicate detection."""

from __future__ import annotations

from collections.abc import Iterable

from .manifest import PluginManifest


class PluginCatalog:
    def __init__(self, manifests: Iterable[PluginManifest] = ()) -> None:
        self._items: dict[str, PluginManifest] = {}
        for manifest in manifests:
            self.add(manifest)

    def add(self, manifest: PluginManifest) -> None:
        previous = self._items.get(manifest.id)
        if previous is not None:
            raise ValueError(
                f"plugin id conflict: {manifest.id!r} ({previous.source} vs {manifest.source})"
            )
        self._items[manifest.id] = manifest

    def get(self, plugin_id: str) -> PluginManifest | None:
        return self._items.get(plugin_id)

    def require(self, plugin_id: str) -> PluginManifest:
        manifest = self.get(plugin_id)
        if manifest is None:
            raise KeyError(plugin_id)
        return manifest

    def values(self) -> tuple[PluginManifest, ...]:
        return tuple(self._items.values())

    def snapshot(self) -> tuple[PluginManifest, ...]:
        return tuple(self._items.values())

    def __contains__(self, plugin_id: str) -> bool:
        return plugin_id in self._items

