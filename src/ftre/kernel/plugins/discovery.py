"""Safe candidate discovery: external modules are not imported until enabled."""
# 中文说明：Plugin Discovery：只读取候选 Manifest；未显式启用的外部模块不会被 import。

from __future__ import annotations

import importlib
import importlib.metadata
import os
import sys
from pathlib import Path
from typing import Any

from .catalog import PluginCatalog
from .manifest import PluginManifest


class PluginDiscovery:
    """Catalog configured candidates and resolve entries only after enablement."""
    def __init__(self, *, plugins_dir: Path | None = None) -> None:
        default_root = Path(os.environ.get("USERPROFILE", Path.home())) / ".ftre" / "plugins"
        self.plugins_dir = Path(plugins_dir) if plugins_dir else default_root

    def catalog(self, builtins: list[PluginManifest], config: dict[str, Any] | None = None) -> PluginCatalog:
        """Merge built-ins and installed entry points without importing modules.

        ``ftre.plugins`` 是发行物的发现边界：读取 entry point 只拿元数据，
        真正的模块导入仍延迟到用户显式启用之后。内置同名 Manifest 优先，
        这样仓库内的默认 Composition 与已安装 wheel 不会产生第二个 Plugin id。
        """
        catalog = PluginCatalog(builtins)
        self._add_installed_entry_points(catalog)
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
            # Disabled external entries are still valid configuration.  Do not
            # require or resolve their entry point: this lets users keep an
            # older plugin declaration disabled while migrating to the current
            # ``module:attribute`` contract.
            if bool(item.get("disabled", False)) or item.get("enabled") is False:
                continue
            if catalog.get(plugin_id) is not None:
                # Builtin config entries are overrides, not a second candidate.
                continue
            entry = item.get("entry")
            if not entry:
                raise ValueError(f"external plugin {plugin_id!r} requires entry")
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

    @staticmethod
    def _add_installed_entry_points(catalog: PluginCatalog) -> None:
        """把已安装发行物登记为可选候选，不在发现阶段 import 代码。"""
        entries = importlib.metadata.entry_points()
        # Python 3.12 返回 SelectableGroups；旧版本/测试替身可能直接返回列表。
        selected = entries.select(group="ftre.plugins") if hasattr(entries, "select") else entries
        for entry in selected:
            plugin_id = str(entry.name).strip()
            if not plugin_id or catalog.get(plugin_id) is not None:
                continue
            distribution = getattr(entry, "dist", None)
            metadata = getattr(distribution, "metadata", {}) if distribution else {}
            catalog.add(
                PluginManifest(
                    id=plugin_id,
                    entry=str(entry.value),
                    source=f"external:{plugin_id}",
                    required=False,
                    default_enabled=False,
                    version=getattr(distribution, "version", None),
                    description=str(metadata.get("Summary", "")),
                )
            )

    def resolve(self, manifest: PluginManifest) -> Any:
        """Resolve a selected entry.  This is intentionally called post-enable."""
        # Resolution is deliberately separate from cataloging: a disabled
        # plugin must not execute import-time code or mutate ``sys.path``.
        entry = manifest.entry
        if not isinstance(entry, str):
            return entry
        module_name, separator, attribute = entry.partition(":")
        if not separator or not module_name or not attribute:
            raise ValueError(f"plugin entry must use 'module:attribute': {entry!r}")
        if self.plugins_dir is not None:
            candidate = (self.plugins_dir / module_name.split(".")[0]).resolve()
            root = self.plugins_dir.resolve()
            if candidate.exists() and root not in candidate.parents and candidate != root:
                raise ValueError(f"plugin entry escapes plugins root: {entry!r}")
            if root.exists() and str(root) not in sys.path:
                sys.path.insert(0, str(root))
            if candidate.is_dir() and str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
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
