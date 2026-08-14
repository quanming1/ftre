"""Configuration-tree driven built-in and external plugin loader."""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .context import FtreContext
from .registry import Plugin, PluginRegistry

logger = logging.getLogger(__name__)

PLUGINS_DIR = Path(os.environ.get("USERPROFILE", Path.home())) / ".ftre" / "plugins"


class Entry(BaseModel):
    id: str | None = None
    name: str | None = None
    module: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool | None = None
    disabled: bool = False
    group: bool = False
    children: list[Entry] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_legacy_enabled(self) -> Entry:
        if self.enabled is False:
            self.disabled = True
        return self

    @property
    def runtime_id(self) -> str:
        value = self.id or self.name
        if not value:
            raise ValueError("plugin entry requires id or name")
        return value


class EntryGroup(Entry):
    group: bool = True


class EntryTree(BaseModel):
    entries: list[Entry] = Field(default_factory=list)

    @classmethod
    def from_config(cls, config_data: dict | None) -> EntryTree:
        raw = (config_data or {}).get("plugins", [])
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise TypeError("config.plugins must be a list")
        return cls(entries=[Entry.model_validate(item) for item in raw])


class PluginLoader:
    """Discover plugins, apply an EntryTree, and own their runtime registry."""

    def __init__(
        self,
        context: FtreContext,
        config_data: dict | None = None,
        *,
        plugins_dir: Path | None = None,
    ) -> None:
        self.context = context
        self.config_data = config_data or {}
        self.plugins_dir = plugins_dir or PLUGINS_DIR
        self.registry = PluginRegistry(context)
        self.tree = EntryTree.from_config(self.config_data)
        self._classes: dict[str, type[Plugin]] = {}

    async def load(self) -> None:
        self._classes = self.discover()
        referenced = self._collect_referenced_names(self.tree.entries)
        for entry in self.tree.entries:
            await self._load_entry(entry, self.context)

        # Preserve the old flat-scanner behavior: built-ins/external plugins omitted
        # from config still load with default config. An explicit disabled entry wins.
        for name, plugin_cls in self._classes.items():
            if name not in referenced:
                await self.registry.register(plugin_cls, {}, instance_id=name)
        await self.registry.drain()

    async def unload(self, runtime_id: str) -> bool:
        return await self.registry.unload(runtime_id)

    async def close(self) -> None:
        await self.registry.close()

    def discover(self) -> dict[str, type[Plugin]]:
        classes: dict[str, type[Plugin]] = {}
        builtin_package = importlib.import_module("ftre.plugin.builtin")
        for info in pkgutil.iter_modules(builtin_package.__path__):
            if info.name.startswith("_"):
                continue
            module = importlib.import_module(f"ftre.plugin.builtin.{info.name}")
            self._collect_module_plugins(module, classes)

        if not self.plugins_dir.is_dir():
            return classes
        parent = str(self.plugins_dir)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        for child in sorted(self.plugins_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("_"):
                continue
            if not (child / "__init__.py").is_file():
                continue
            try:
                child_path = str(child)
                if child_path not in sys.path:
                    sys.path.insert(0, child_path)
                module = importlib.import_module(child.name)
                self._collect_module_plugins(module, classes, require_local=False)
            except Exception:
                logger.exception("[plugin] external discovery failed: %s", child.name)
        return classes

    @property
    def routers(self) -> list[Any]:
        routers = self.context.get("routers", strict=False)
        return list(routers or [])

    @property
    def tool_registry(self) -> Any:
        return self.context.get("tool_registry", strict=False)

    def tools(self) -> list[Any]:
        registry = self.tool_registry
        return registry.snapshot() if registry is not None else []

    def list(self) -> list[dict[str, str]]:
        return self.registry.list()

    async def _load_entry(self, entry: Entry, scope: FtreContext) -> None:
        if entry.disabled:
            logger.info("[plugin] disabled entry skipped: %s", entry.runtime_id)
            return
        if entry.group:
            child_scope = scope.extend()
            for child in entry.children:
                await self._load_entry(child, child_scope)
            return
        try:
            plugin_cls = self._resolve_class(entry)
        except (AttributeError, LookupError, ModuleNotFoundError) as exc:
            # The legacy PluginManager treated config entries only as optional
            # configuration for discovered plugins.  Stale entries therefore did
            # not prevent the gateway from starting.  Preserve that behaviour for
            # existing user config while still rejecting malformed classes/config.
            logger.warning(
                "[plugin] configured entry unavailable, skipped: id=%s (%s)",
                entry.runtime_id,
                exc,
            )
            return
        await self.registry.register(
            plugin_cls,
            entry.config,
            scope=scope,
            instance_id=entry.runtime_id,
        )

    def _resolve_class(self, entry: Entry) -> type[Plugin]:
        # A discovered plugin name is authoritative.  Older config files may carry
        # a now-stale ``module`` hint that the former flat scanner ignored.
        if entry.name and entry.name in self._classes:
            return self._classes[entry.name]
        if entry.module:
            module_name, separator, class_name = entry.module.rpartition(".")
            if not separator:
                raise ValueError(
                    f"plugin entry {entry.runtime_id!r} module must include a class name"
                )
            module = importlib.import_module(module_name)
            plugin_cls = getattr(module, class_name)
            if not isinstance(plugin_cls, type) or not issubclass(plugin_cls, Plugin):
                raise TypeError(f"{entry.module!r} is not a Plugin subclass")
            return plugin_cls
        if not entry.name:
            raise ValueError(
                f"plugin entry {entry.runtime_id!r} requires name or module"
            )
        raise LookupError(f"plugin {entry.name!r} was not discovered")

    @staticmethod
    def _collect_module_plugins(
        module: Any,
        output: dict[str, type[Plugin]],
        *,
        require_local: bool = True,
    ) -> None:
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, Plugin)
                and value is not Plugin
                and (not require_local or value.__module__ == module.__name__)
                and value.name
            ):
                output.setdefault(value.name, value)

    @classmethod
    def _collect_referenced_names(cls, entries: list[Entry]) -> set[str]:
        names: set[str] = set()
        for entry in entries:
            if entry.name:
                names.add(entry.name)
            names.update(cls._collect_referenced_names(entry.children))
        return names


Entry.model_rebuild()
