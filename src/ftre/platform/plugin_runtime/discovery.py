"""Safe candidate discovery: external modules are not imported until enabled."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

from .catalog import PluginCatalog
from .manifest import PluginManifest


class PluginDiscovery:
    def __init__(self, *, plugins_dir: Path | None = None) -> None:
        self.plugins_dir = Path(plugins_dir) if plugins_dir else None

    def catalog(self, builtins: list[PluginManifest], config: dict[str, Any] | None = None) -> PluginCatalog:
        catalog = PluginCatalog(builtins)
        raw = (config or {}).get("plugins", [])
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise TypeError("config.plugins must be a list")
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise TypeError("each config.plugins entry must be an object")
            plugin_id = str(item.get("id") or item.get("name") or "").strip()
            if not plugin_id:
                raise ValueError("external plugin entry requires id/name")
            if plugin_id in seen:
                raise ValueError(f"duplicate plugin config entry: {plugin_id}")
            seen.add(plugin_id)
            if catalog.get(plugin_id) is not None:
                # Builtin config entries are overrides, not a second candidate.
                continue
            entry = item.get("entry") or item.get("module")
            if not entry:
                raise ValueError(f"external plugin {plugin_id!r} requires entry/module")
            source = f"external:{plugin_id}"
            catalog.add(
                PluginManifest(
                    id=plugin_id,
                    entry=str(entry),
                    source=source,
                    required=bool(item.get("required", False)),
                    default_enabled=False,
                    version=item.get("version"),
                    description=str(item.get("description", "")),
                    config=dict(item.get("config") or {}),
                )
            )
        return catalog

    def resolve(self, manifest: PluginManifest) -> Any:
        """Resolve a selected entry.  This is intentionally called post-enable."""
        entry = manifest.entry
        if not isinstance(entry, str):
            return entry
        module_name, separator, attribute = entry.partition(":")
        if not separator:
            module_name, separator, attribute = entry.rpartition(".")
        if not separator or not module_name or not attribute:
            raise ValueError(f"invalid plugin entry: {entry!r}")
        if self.plugins_dir is not None:
            candidate = (self.plugins_dir / module_name.split(".")[0]).resolve()
            root = self.plugins_dir.resolve()
            if candidate.exists() and root not in candidate.parents and candidate != root:
                raise ValueError(f"plugin entry escapes plugins root: {entry!r}")
            if root.exists() and str(root) not in sys.path:
                sys.path.insert(0, str(root))
        module = importlib.import_module(module_name)
        try:
            target = getattr(module, attribute)
            # Python modules commonly keep the declarative contract next to the
            # function entry.  Copy it onto the resolved callable so Cordis
            # sees the same ``inject``/``provide`` metadata as class plugins.
            for key in ("inject", "provide"):
                if hasattr(module, key) and not hasattr(target, key):
                    setattr(target, key, getattr(module, key))
            return target
        except AttributeError as exc:
            raise LookupError(f"plugin attribute not found: {entry!r}") from exc
