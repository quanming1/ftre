"""Scoped service context and lifecycle-aware capability adapters."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .events import EventHub

if TYPE_CHECKING:
    from fastapi import APIRouter
    from ftre_agent_core.tool import Tool

    from ftre.channel import Channel

    from .lifecycle import PluginInstance
    from .registry import PluginRegistry


_MISSING = object()
Cleanup = Callable[[], Any]


class ServiceAccessError(PermissionError):
    """Raised when a plugin accesses a service it did not declare in ``inject``."""


class FtreContext:
    """A hierarchical service scope shared by a group of plugins.

    Root/group contexts own service dictionaries. A plugin receives a bound view of
    its group scope; the bound view enforces declared dependencies and records every
    registration as a lifecycle effect.
    """

    def __init__(
        self,
        parent: FtreContext | None = None,
        *,
        events: EventHub | None = None,
        _scope: FtreContext | None = None,
        _owner: PluginInstance | None = None,
        _allowed: set[str] | None = None,
    ) -> None:
        if _scope is not None:
            self.parent = _scope.parent
            self._scope = _scope
            self._services = _scope._services
            self.events = _scope.events
            self._root = _scope._root
        else:
            self.parent = parent
            self._scope = self
            self._services: dict[str, Any] = {}
            self.events = events or (parent.events if parent else EventHub())
            self._root = parent._root if parent else self
        self._owner = _owner
        self._allowed = _allowed
        self._registry: PluginRegistry | None = None
        self._proxy_cache: dict[str, Any] = {}

    def extend(self) -> FtreContext:
        """Create an isolated child service scope sharing the same event hub."""
        return FtreContext(parent=self._scope, events=self.events)

    def bind(
        self,
        owner: PluginInstance,
        allowed: set[str],
    ) -> FtreContext:
        """Create the restricted context view passed to one plugin instance."""
        return FtreContext(_scope=self._scope, _owner=owner, _allowed=set(allowed))

    def attach_registry(self, registry: PluginRegistry) -> None:
        self._root._registry = registry

    async def use(
        self,
        plugin: type,
        config: dict | None = None,
        *,
        instance_id: str | None = None,
    ) -> PluginInstance:
        """Register a plugin in this scope and bind nested lifetime to its owner."""
        registry = self._root._registry
        if registry is None:
            raise RuntimeError("no plugin registry is attached to this context")
        nested = self._owner is not None
        instance = await registry.register(
            plugin,
            config,
            scope=self._scope,
            instance_id=instance_id,
            _defer_reconcile=nested,
        )
        if nested:
            self.effect(lambda: registry.unload(instance.runtime_id))
            registry.service_changed()
        return instance

    # -- services ---------------------------------------------------------

    def provide(self, name: str, value: Any) -> Cleanup:
        """Provide a service in this scope and return an idempotent disposer."""
        name = (name or "").strip()
        if not name:
            raise ValueError("service name must not be empty")
        if self._owner is not None and name not in self._owner.provided_services:
            raise ServiceAccessError(
                f"plugin {self._owner.name!r} did not declare provide={name!r}"
            )
        target = self._scope
        if name in target._services:
            raise ValueError(f"service {name!r} is already provided in this scope")
        target._services[name] = value
        active = True

        def dispose() -> bool:
            nonlocal active
            if not active or target._services.get(name, _MISSING) is not value:
                return False
            active = False
            del target._services[name]
            target._notify_service_change()
            return True

        if self._owner is not None:
            self.effect(dispose)
        target._notify_service_change()
        return dispose

    def get(self, name: str, *, strict: bool = True) -> Any:
        self._check_access(name)
        scope: FtreContext | None = self._scope
        while scope is not None:
            if name in scope._services:
                return scope._services[name]
            scope = scope.parent
        if strict:
            raise KeyError(f"service {name!r} is not available")
        return None

    def has(self, name: str) -> bool:
        self._check_access(name)
        return self._lookup(name) is not _MISSING

    def _lookup(self, name: str) -> Any:
        scope: FtreContext | None = self._scope
        while scope is not None:
            if name in scope._services:
                return scope._services[name]
            scope = scope.parent
        return _MISSING

    def _check_access(self, name: str) -> None:
        if (
            self._owner is not None
            and self._allowed is not None
            and name not in self._allowed
        ):
            raise ServiceAccessError(
                f"plugin {self._owner.name!r} must declare inject={name!r} before access"
            )

    def _notify_service_change(self) -> None:
        registry = self._root._registry
        if registry is not None:
            registry.service_changed()

    # -- effects/events --------------------------------------------------

    def effect(self, cleanup: Cleanup) -> Cleanup:
        if not callable(cleanup):
            raise TypeError("cleanup must be callable")
        if self._owner is None:
            raise RuntimeError("effects can only be registered from a plugin context")
        self._owner.add_effect(cleanup)
        return cleanup

    def on(self, name: str, listener: Callable, *, prepend: bool = False) -> Cleanup:
        disposer = self.events.on(name, listener, prepend=prepend)
        if self._owner is not None:
            self.effect(disposer)
        return disposer

    def once(self, name: str, listener: Callable, *, prepend: bool = False) -> Cleanup:
        disposer = self.events.once(name, listener, prepend=prepend)
        if self._owner is not None:
            self.effect(disposer)
        return disposer

    def emit(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.events.emit(name, *args, **kwargs)

    async def parallel(self, name: str, *args: Any, **kwargs: Any) -> None:
        await self.events.parallel(name, *args, **kwargs)

    async def serial(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return await self.events.serial(name, *args, **kwargs)

    def bail(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return self.events.bail(name, *args, **kwargs)

    async def waterfall(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return await self.events.waterfall(name, *args, **kwargs)

    async def filter(self, name: str, value: Any) -> Any:
        return await self.events.filter(name, value)

    # -- tracked capability adapters ------------------------------------

    @property
    def tool_registry(self) -> _ToolRegistryProxy:
        return self._proxy("tool_registry", _ToolRegistryProxy)

    @property
    def channel_manager(self) -> _ChannelManagerProxy:
        return self._proxy("channel_manager", _ChannelManagerProxy)

    @property
    def command_manager(self) -> _CommandManagerProxy:
        return self._proxy("command_manager", _CommandManagerProxy)

    @property
    def event_loop(self) -> Any:
        value = self.get("event_loop")
        return value() if callable(value) else value

    @property
    def routers(self) -> list[APIRouter]:
        return self.get("routers")

    def register_router(self, router: APIRouter) -> None:
        routers = self.routers
        routers.append(router)

        def cleanup() -> bool:
            try:
                routers.remove(router)
                return True
            except ValueError:
                return False

        self.effect(cleanup)

    def register_channel(self, channel: Channel) -> None:
        self.channel_manager.register(channel)

    def register_core_hook(self, point: str, fn: Callable) -> None:
        manager = self.get("core_hook_manager")
        manager.register(point, fn)
        self.effect(lambda: manager.unregister(point, fn))

    def _proxy(self, service_name: str, proxy_type: type) -> Any:
        value = self.get(service_name)
        cached = self._proxy_cache.get(service_name)
        if cached is None or cached._target is not value:
            cached = proxy_type(value, self)
            self._proxy_cache[service_name] = cached
        return cached

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        value = self.get(name, strict=False)
        if value is None and self._lookup(name) is _MISSING:
            raise AttributeError(name)
        return value


class _ProxyBase:
    def __init__(self, target: Any, ctx: FtreContext) -> None:
        self._target = target
        self._ctx = ctx

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


class _ToolRegistryProxy(_ProxyBase):
    def register(self, tool: Tool) -> None:
        previous = self._target.get(tool.name)
        self._target.register(tool)

        def cleanup() -> None:
            if self._target.get(tool.name) is not tool:
                return
            self._target.unregister(tool.name)
            if previous is not None:
                self._target.register(previous)

        self._ctx.effect(cleanup)

    def unregister(self, name: str) -> None:
        self._target.unregister(name)


class _ChannelManagerProxy(_ProxyBase):
    def register(self, channel: Channel) -> None:
        previous = self._target.get(channel.channel_id)
        self._target.register(channel)

        async def cleanup() -> Any:
            current = self._target.get(channel.channel_id)
            if current is not channel:
                return False
            stop = getattr(channel, "stop", None)
            if callable(stop):
                result = stop()
                if inspect.isawaitable(result):
                    await result
            result = self._target.unregister(channel.channel_id)
            if previous is not None:
                self._target.register(previous)
            return result

        self._ctx.effect(cleanup)


class _CommandManagerProxy(_ProxyBase):
    def register(
        self, command: str, handler: Callable, **kwargs: Any
    ) -> _CommandManagerProxy:
        self._target.register(command, handler, **kwargs)
        self._ctx.effect(lambda: self._target.unregister(command))
        return self

    def register_def(self, command_def: Any) -> _CommandManagerProxy:
        self._target.register_def(command_def)
        self._ctx.effect(lambda: self._target.unregister(command_def.command))
        return self


async def run_cleanup(cleanup: Cleanup) -> Any:
    """Execute a sync/async cleanup callable."""
    result = cleanup()
    if inspect.isawaitable(result):
        return await result
    return result
