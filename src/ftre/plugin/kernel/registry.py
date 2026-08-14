"""Plugin declarations, dependency graph, and runtime registry."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from .context import FtreContext
    from .lifecycle import PluginInstance

logger = logging.getLogger(__name__)


class PluginDependencyCycleError(RuntimeError):
    """Raised when declared plugin dependencies form a cycle."""


class Plugin:
    """Base class for a lifecycle-managed plugin."""

    name = ""
    version = "0.0.0"
    inject: ClassVar[list[str] | tuple[str, ...]] = ()
    provide: ClassVar[str | list[str] | tuple[str, ...]] = ()
    Config: ClassVar[type[BaseModel] | None] = None

    async def setup(self, ctx: FtreContext, config: Any) -> Callable[[], Any] | None:
        raise NotImplementedError

    @classmethod
    def provided_services(cls) -> set[str]:
        value = cls.provide
        if isinstance(value, str):
            return {value} if value else set()
        return {str(name) for name in value if str(name)}

    @classmethod
    def validate_config(cls, raw: dict | None) -> Any:
        data = raw or {}
        if cls.Config is None:
            return data
        validator = getattr(cls.Config, "model_validate", None)
        if validator is not None:
            return validator(data)
        return cls.Config.parse_obj(data)


class PluginRegistry:
    """Owns plugin instances and reconciles them against scoped services."""

    def __init__(self, context: FtreContext) -> None:
        self.context = context
        self.context.attach_registry(self)
        self._instances: dict[str, PluginInstance] = {}
        self._reconcile_lock = asyncio.Lock()
        self._scheduled: set[asyncio.Task] = set()
        self._closed = False

    @property
    def instances(self) -> dict[str, PluginInstance]:
        return dict(self._instances)

    async def register(
        self,
        plugin: type[Plugin] | Plugin,
        config: dict | None = None,
        *,
        scope: FtreContext | None = None,
        instance_id: str | None = None,
        _defer_reconcile: bool = False,
    ) -> PluginInstance:
        from .lifecycle import PluginInstance

        plugin_obj = plugin() if isinstance(plugin, type) else plugin
        if not isinstance(plugin_obj, Plugin):
            raise TypeError("plugin must be a Plugin subclass or instance")
        if not plugin_obj.name:
            raise ValueError("plugin name must not be empty")
        runtime_id = instance_id or plugin_obj.name
        if runtime_id in self._instances:
            raise ValueError(f"plugin instance {runtime_id!r} is already registered")
        target_scope = (scope or self.context)._scope
        self._assert_unique_providers(plugin_obj, target_scope)
        validated_config = type(plugin_obj).validate_config(config)
        instance = PluginInstance(
            runtime_id=runtime_id,
            plugin=plugin_obj,
            scope=target_scope,
            config=validated_config,
            registry=self,
        )
        self._instances[runtime_id] = instance
        try:
            self._assert_acyclic()
        except Exception:
            del self._instances[runtime_id]
            raise
        if not _defer_reconcile:
            await self.resolve_pending()
        return instance

    async def resolve_pending(self) -> None:
        if self._closed:
            return
        async with self._reconcile_lock:
            # First remove capabilities whose dependencies disappeared.
            changed = True
            while changed:
                changed = False
                for instance in reversed(list(self._instances.values())):
                    if instance.is_active and not self._dependencies_ready(instance):
                        await instance.deactivate()
                        changed = True

            # Then activate ready plugins. A setup may provide a dependency for the
            # next pending plugin, so repeat until no additional plugin activates.
            while True:
                progressed = False
                for instance in list(self._instances.values()):
                    if instance.is_pending and self._dependencies_ready(instance):
                        await instance.activate()
                        progressed = True
                if not progressed:
                    break

    def service_changed(self) -> None:
        """Schedule dependency reconciliation after a provide/remove operation."""
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.resolve_pending())
        self._scheduled.add(task)
        task.add_done_callback(self._scheduled.discard)

    async def drain(self) -> None:
        """Wait until all service-change reconciliation tasks settle."""
        while self._scheduled:
            await asyncio.gather(*tuple(self._scheduled))

    async def unload(self, runtime_id: str) -> bool:
        instance = self._instances.get(runtime_id)
        if instance is None:
            return False
        await self._unload_dependents(instance, seen=set())
        await instance.dispose()
        del self._instances[runtime_id]
        await self.resolve_pending()
        return True

    async def close(self) -> None:
        if self._closed:
            return
        for runtime_id in reversed(list(self._instances)):
            instance = self._instances.get(runtime_id)
            if instance is not None:
                await instance.dispose()
        self._instances.clear()
        self._closed = True

    def list(self) -> list[dict[str, str]]:
        return [
            {
                "id": instance.runtime_id,
                "name": instance.name,
                "version": instance.plugin.version,
                "state": instance.state.value,
            }
            for instance in self._instances.values()
        ]

    def _dependencies_ready(self, instance: PluginInstance) -> bool:
        return all(
            instance.scope._lookup(name) is not _MISSING for name in instance.inject
        )

    def _assert_unique_providers(self, plugin: Plugin, scope: FtreContext) -> None:
        declared = type(plugin).provided_services()
        for other in self._instances.values():
            if other.scope is scope and declared.intersection(other.provided_services):
                duplicate = min(declared.intersection(other.provided_services))
                raise ValueError(
                    f"service {duplicate!r} has multiple providers in one scope"
                )

    def _assert_acyclic(self) -> None:
        graph: dict[str, set[str]] = {key: set() for key in self._instances}
        for runtime_id, instance in self._instances.items():
            for service in instance.inject:
                provider = self._find_provider(instance, service)
                if provider is not None:
                    graph[runtime_id].add(provider.runtime_id)

        visiting: list[str] = []
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                start = visiting.index(node)
                cycle = visiting[start:] + [node]
                raise PluginDependencyCycleError(" -> ".join(cycle))
            if node in visited:
                return
            visiting.append(node)
            for child in graph[node]:
                visit(child)
            visiting.pop()
            visited.add(node)

        for node in graph:
            visit(node)

    def _find_provider(
        self, consumer: PluginInstance, service: str
    ) -> PluginInstance | None:
        scope = consumer.scope
        while scope is not None:
            for candidate in self._instances.values():
                if candidate.scope is scope and service in candidate.provided_services:
                    return candidate
            scope = scope.parent
        return None

    async def _unload_dependents(
        self, provider: PluginInstance, seen: set[str]
    ) -> None:
        if provider.runtime_id in seen:
            return
        seen.add(provider.runtime_id)
        for dependent in reversed(list(self._instances.values())):
            if dependent is provider or not dependent.is_active:
                continue
            if self._depends_on(dependent, provider):
                await self._unload_dependents(dependent, seen)
                await dependent.deactivate()

    def _depends_on(self, consumer: PluginInstance, provider: PluginInstance) -> bool:
        return any(
            self._find_provider(consumer, name) is provider for name in consumer.inject
        )


# Imported lazily above to avoid context -> registry import cycles.
from .context import _MISSING
