"""配置 Service：``~/.ftre/config.json`` 的唯一 Owner。

Service 同时维护内存快照、revision 和 watcher；持久化由 ``JsonConfigStore`` 完成。
调用方只拿不可变快照或提交 patch，不能直接持有内部 dict，这样 optimistic
concurrency 和原子写入才有单一语义。
"""

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
    """提交者基于旧 revision 写入时抛出的并发冲突。"""
    code = "config_revision_conflict"


@dataclass(frozen=True)
class ConfigSnapshot:
    """一次配置提交后的防御性快照。"""
    revision: int
    value: dict[str, Any]


class ConfigService:
    """拥有配置内存、原子持久化、revision 和 watcher 回调。"""
    key = "config"

    def __init__(self, path: Path | str = CONFIG_PATH, initial: dict[str, Any] | None = None) -> None:
        self._store = JsonConfigStore(Path(path))
        self._value = copy.deepcopy(initial) if initial is not None else self._store.read()
        self._revision = 0
        self._watchers: list[Callable[[ConfigSnapshot], Any]] = []

    @property
    def path(self) -> Path:
        """返回配置文件路径，供诊断显示，不允许调用方替换 Store。"""
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

    def resolve_llm(self, provider_name: str, model_id: str) -> dict[str, Any] | None:
        """Resolve a provider/model pair for an LLM Plugin.

        这是 ConfigService 对外公开的最小模型解析边界：调用方只能拿到一次
        防御性快照，不能访问内部配置字典或 AgentProfile。返回值包含构造
        Core Adapter 所需的协议和凭据，但本方法不记录、不打印 API key。
        未找到 provider/model 时返回 ``None``，由调用方决定是否放弃可选能力。
        """
        if not isinstance(provider_name, str) or not provider_name:
            return None
        if not isinstance(model_id, str) or not model_id:
            return None
        provider = self._value.get("providers", {}).get(provider_name, {})
        if not isinstance(provider, dict):
            return None
        models = provider.get("models", ())
        if not isinstance(models, (list, tuple)):
            return None
        model_entry = next(
            (
                item
                for item in models
                if isinstance(item, dict) and item.get("id") == model_id
            ),
            None,
        )
        if model_entry is None:
            return None
        raw_api_type = model_entry.get("api_type") or provider.get("api_type") or "completions"
        result: dict[str, Any] = {
            "provider": provider_name,
            "model": model_id,
            "api_key": provider.get("api_key", ""),
            "api_base": provider.get("api_base", ""),
            "api_type": raw_api_type if isinstance(raw_api_type, str) else "completions",
            "reasoning_effort": model_entry.get("reasoning_effort", ""),
            # 模型能力也属于解析快照；Compaction、Vision 和 Retry/Fallback
            # 不能再各自读取 config.json 造成能力判断不一致。
            "vision": bool(model_entry.get("vision", False)),
        }
        context_window = model_entry.get("context_window")
        if isinstance(context_window, int) and context_window > 0:
            result["context_window"] = context_window
        max_output = model_entry.get("max_output")
        if isinstance(max_output, int) and max_output > 0:
            result["max_output"] = max_output
        raw_effort_values = model_entry.get("reasoning_effort_values")
        if isinstance(raw_effort_values, (list, tuple)):
            result["reasoning_effort_values"] = tuple(
                item for item in raw_effort_values if isinstance(item, str)
            )
        return result

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
