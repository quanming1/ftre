"""Single owner of ~/.ftre/config.json and its revisioned snapshots."""

from __future__ import annotations

import copy
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import CONFIG_PATH
from .store import JsonConfigStore

logger = logging.getLogger(__name__)


class ConfigConflictError(RuntimeError):
    code = "config_revision_conflict"


@dataclass(frozen=True)
class ConfigSnapshot:
    revision: int
    value: dict[str, Any]


class ConfigService:
    """Own config memory, atomic persistence, revisions, and watcher callbacks."""
    key = "config"

    def __init__(self, path: Path | str = CONFIG_PATH, initial: dict[str, Any] | None = None) -> None:
        self._store = JsonConfigStore(Path(path))
        self._value = copy.deepcopy(initial) if initial is not None else self._store.read()
        self._revision = 0
        self._watchers: list[Callable[[ConfigSnapshot], Any]] = []

    @property
    def path(self) -> Path:
        return self._store.path

    def snapshot(self) -> ConfigSnapshot:
        """Return a defensive copy so callers cannot mutate service state."""
        return ConfigSnapshot(self._revision, copy.deepcopy(self._value))

    def plugin_config(self, plugin_id: str) -> dict[str, Any]:
        """Read one plugin's nested config without exposing the full config object."""
        entries = self._value.get("plugins", [])
        if not isinstance(entries, list):
            return {}
        for entry in entries:
            if isinstance(entry, dict) and (entry.get("id") or entry.get("name")) == plugin_id:
                config = entry.get("config", {})
                return copy.deepcopy(config) if isinstance(config, dict) else {}
        return {}

    def watch(self, callback: Callable[[ConfigSnapshot], Any]) -> Callable[[], bool]:
        """Subscribe to committed snapshots and return an idempotent disposer."""
        self._watchers.append(callback)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            try:
                self._watchers.remove(callback)
            except ValueError:
                return False
            return True

        return dispose

    async def update(self, patch: dict[str, Any], expected_revision: int | None = None) -> ConfigSnapshot:
        """Deep-merge a patch and commit it with optional optimistic concurrency."""
        if not isinstance(patch, dict):
            raise TypeError("config patch must be an object")
        candidate = _deep_merge(self._value, patch)
        return await self._commit(candidate, expected_revision)

    async def replace(self, value: dict[str, Any], expected_revision: int | None = None) -> ConfigSnapshot:
        """Atomically replace the complete config and notify watchers."""
        if not isinstance(value, dict):
            raise TypeError("config must be an object")
        return await self._commit(value, expected_revision)

    def replace_sync(self, value: dict[str, Any], expected_revision: int | None = None) -> ConfigSnapshot:
        """Synchronous compatibility entry for legacy async-route helpers."""
        if not isinstance(value, dict):
            raise TypeError("config must be an object")
        if expected_revision is not None and expected_revision != self._revision:
            raise ConfigConflictError(f"expected revision {expected_revision}, current {self._revision}")
        self._store.write_atomic(value)
        self._value = copy.deepcopy(value)
        self._revision += 1
        return self.snapshot()

    async def _commit(self, candidate: dict[str, Any], expected_revision: int | None) -> ConfigSnapshot:
        if expected_revision is not None and expected_revision != self._revision:
            raise ConfigConflictError(f"expected revision {expected_revision}, current {self._revision}")
        self._store.write_atomic(candidate)
        self._value = copy.deepcopy(candidate)
        self._revision += 1
        snapshot = self.snapshot()
        for callback in tuple(self._watchers):
            try:
                result = callback(snapshot)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("config watcher failed")
        return snapshot


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result
