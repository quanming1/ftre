"""Small, dependency-free Cordis-compatible runtime surface used by ftre.

The upstream Python distribution is not available in every deployment image.
This module intentionally implements only the stable primitives ftre consumes:
Context, Fiber, Service, Inject and Effect.  It is kept independent from ftre
so external plugins can target the public contract without importing an ftre
private implementation.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Self

logger = logging.getLogger(__name__)

Cleanup = Callable[[], Any]


class FiberState(str, Enum):
    PENDING = "PENDING"
    LOADING = "LOADING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    UNLOADING = "UNLOADING"
    DISPOSED = "DISPOSED"


class Inject(tuple[str, ...]):
    """Declarative service dependency list.

    ``Inject("config", "sessions")`` is deliberately just a tuple at runtime,
    so normal plugin declarations such as ``inject = ("config",)`` work too.
    """

    def __new__(cls, *names: str) -> Self:
        return tuple.__new__(cls, tuple(str(name) for name in names))


@dataclass(frozen=True)
class Effect:
    """Metadata for a reversible side effect."""

    cleanup: Cleanup
    label: str = "effect"

    def __call__(self) -> Any:
        return self.cleanup()


class Service:
    """Optional base class for stateful providers.

    ftre providers may use ``ctx.provide`` directly; this class exists for
    plugins that prefer an explicit named Service object.
    """

    name: ClassVar[str] = ""

    def __init__(self, ctx: Context, name: str | None = None) -> None:
        self.ctx = ctx
        if name is not None:
            self.name = name


class ServiceAccessError(PermissionError):
    """Raised when a restricted plugin context accesses an undeclared service."""


@dataclass
class _FiberRecord:
    fiber: Fiber
    order: int


class PluginContext:
    """A context view bound to one Fiber."""

    def __init__(self, root: Context, fiber: Fiber) -> None:
        self._root = root
        self._fiber = fiber

    @property
    def fiber(self) -> Fiber:
        return self._fiber

    def get(self, name: str, strict: bool = True) -> Any:
        self._check(name)
        value = self._root.get(name, strict=False)
        if value is None and strict:
            raise KeyError(f"service {name!r} is not available")
        return value

    def has(self, name: str) -> bool:
        self._check(name)
        return self._root.get(name, strict=False) is not None

    def optional(self, name: str) -> Any:
        """Read an already-provided value while implementing a provider.

        Provider Plugins use this only to support Composition injection of an
        application-owned instance.  Consumers must continue to declare
        ``inject`` and use ``get``/attribute access.
        """
        return self._root.get(name, strict=False)

    def set(self, name: str, value: Any) -> None:
        self._check(name)
        self._root.set(name, value)

    def provide(self, name: str, value: Any) -> Cleanup:
        self._check_provide(name)
        disposer = self._root.provide(name, value, owner=self._fiber)
        self.effect(disposer, label=f"service:{name}")
        return disposer

    def effect(self, cleanup: Cleanup, *, label: str = "effect") -> Cleanup:
        if not callable(cleanup):
            raise TypeError("cleanup must be callable")
        self._fiber.add_effect(Effect(cleanup, label))
        return cleanup

    def on(self, event: str, callback: Callable[..., Any]) -> Cleanup:
        disposer = self._root.on(event, callback)
        self.effect(disposer, label=f"event:{event}")
        return disposer

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        self._root.emit(event, *args, **kwargs)

    def plugin(self, plugin: Any, config: Any = None, *, id: str | None = None) -> Fiber:
        return self._root.plugin(plugin, config, id=id, parent=self._fiber)

    def inject(self, names: Iterable[str], callback: Callable[..., Any]) -> Fiber:
        names = tuple(names)
        class _Injected:
            inject = tuple(names)
            name = getattr(callback, "__name__", "injected")

            @staticmethod
            async def apply(ctx: PluginContext, config: Any = None) -> Any:
                result = callback(ctx)
                if inspect.isawaitable(result):
                    return await result
                return result

        return self.plugin(_Injected, {})

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self.get(name)

    def _check(self, name: str) -> None:
        if name not in self._fiber.inject:
            raise ServiceAccessError(
                f"plugin {self._fiber.plugin_id!r} must declare inject={name!r}"
            )

    def _check_provide(self, name: str) -> None:
        if self._fiber.provide and name not in self._fiber.provide:
            raise ServiceAccessError(
                f"plugin {self._fiber.plugin_id!r} did not declare provide={name!r}"
            )


class LegacyPluginContext:
    """Bridge for pre-F1 ``setup(ctx, config)`` plugins.

    This adapter is intentionally narrow and only exists so installed Octo and
    other old plugins can migrate without a flag day.  New plugins receive
    PluginContext and must use public Service keys directly.
    """

    _ALIASES: ClassVar[dict[str, str]] = {
        "bus": "message_bus",
        "session_manager": "sessions",
        "channel_manager": "channels",
        "tool_registry": "tools",
        "command_manager": "commands",
    }

    def __init__(self, root: Context, fiber: Fiber) -> None:
        self._root = root
        self._fiber = fiber

    def _value(self, name: str) -> Any:
        key = self._ALIASES.get(name, name)
        value = self._root.get(key, strict=False)
        if key == "message_bus" and value is not None:
            return getattr(value, "bus", value)
        if key == "channels" and value is not None:
            return getattr(value, "manager", value)
        if key == "tools" and value is not None:
            return getattr(value, "registry", value)
        if key == "commands" and value is not None:
            return getattr(value, "manager", value)
        if key == "event_loop" and callable(value):
            return value()
        return value

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        value = self._value(name)
        if value is None:
            raise AttributeError(name)
        return value

    def effect(self, cleanup: Cleanup, *, label: str = "legacy-effect") -> Cleanup:
        self._fiber.add_effect(Effect(cleanup, label))
        return cleanup

    def on(self, event: str, callback: Callable[..., Any]) -> Cleanup:
        return self.effect(self._root.on(event, callback), label=f"event:{event}")

    def register_router(self, router: Any) -> None:
        http = self._root.get("http", strict=False)
        if http is None:
            return
        self.effect(http.register_router(router, owner=self._fiber.plugin_id), label="router:legacy")

    def register_channel(self, channel: Any) -> None:
        manager = self._value("channels")
        if manager is None:
            return
        manager.register(channel)

        async def cleanup() -> None:
            stop = getattr(channel, "stop", None)
            if callable(stop):
                result = stop()
                if inspect.isawaitable(result):
                    await result
            manager.unregister(channel.channel_id)

        self.effect(cleanup, label=f"channel:{channel.channel_id}")

    def register_core_hook(self, point: str, callback: Callable[..., Any]) -> None:
        manager = self._root.get("core_hook_manager", strict=False)
        if manager is not None:
            manager.register(point, callback)
            self.effect(lambda: manager.unregister(point, callback), label=f"hook:{point}")


class Fiber:
    """Lifecycle record for one plugin registration."""

    def __init__(
        self,
        root: Context,
        plugin: Any,
        config: Any,
        plugin_id: str,
        order: int,
        parent: Fiber | None = None,
    ) -> None:
        self.root = root
        self.plugin = plugin
        self.config = config
        self.plugin_id = plugin_id
        self.order = order
        self.parent = parent
        self.state = FiberState.PENDING
        self.error: BaseException | None = None
        self.missing: tuple[str, ...] = ()
        self.effects: list[Effect] = []
        self.provided: dict[str, Any] = {}
        self._done = asyncio.Event()
        self.inject = tuple(getattr(plugin, "inject", ()) or ())
        declared = getattr(plugin, "provide", ()) or ()
        self.provide = (declared,) if isinstance(declared, str) else tuple(declared)
        self.context = PluginContext(root, self)

    @property
    def id(self) -> str:
        return self.plugin_id

    def add_effect(self, effect: Effect) -> None:
        self.effects.append(effect)

    def __await__(self):
        return self.wait().__await__()

    async def wait(self, timeout: float | None = None) -> Fiber:
        async def _wait() -> Fiber:
            while self.state in {FiberState.PENDING, FiberState.LOADING}:
                await asyncio.sleep(0)
            return self

        if timeout is None:
            await _wait()
        else:
            await asyncio.wait_for(_wait(), timeout)
        if self.state is FiberState.FAILED and self.error is not None:
            raise self.error
        return self

    async def activate(self) -> None:
        if self.state is not FiberState.PENDING:
            return
        self.missing = tuple(
            name for name in self.inject if self.root.get(name, strict=False) is None
        )
        if self.missing:
            return
        self.state = FiberState.LOADING
        try:
            result = _invoke_plugin(self.plugin, self.context, self.config, self)
            if inspect.isawaitable(result):
                result = await result
            if callable(result):
                self.add_effect(Effect(result, "plugin-return"))
            self.state = FiberState.ACTIVE
            self._done.set()
        except Exception as exc:
            # A partially applied plugin must not leak registrations when its
            # entry point fails.  Cleanup is best-effort so the original
            # startup error remains the diagnostic users see.
            for effect in reversed(self.effects):
                try:
                    result = effect()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception("cordis rollback effect failed: %s (%s)", self.plugin_id, effect.label)
            self.effects.clear()
            self.error = exc
            self.state = FiberState.FAILED
            self._done.set()
            logger.exception("cordis plugin failed: %s", self.plugin_id)

    async def dispose(self) -> None:
        if self.state in {FiberState.DISPOSED, FiberState.UNLOADING}:
            return
        self.state = FiberState.UNLOADING
        errors: list[BaseException] = []
        for effect in reversed(self.effects):
            try:
                result = effect()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                errors.append(exc)
                logger.exception("cordis effect failed: %s (%s)", self.plugin_id, effect.label)
        self.effects.clear()
        self.state = FiberState.DISPOSED
        self._done.set()
        if errors:
            raise ExceptionGroup(f"cleanup failed for {self.plugin_id}", errors)

    async def deactivate(self) -> None:
        """Undo effects while keeping the registration pending for reactivation."""
        if self.state is not FiberState.ACTIVE:
            return
        self.state = FiberState.UNLOADING
        for effect in reversed(self.effects):
            try:
                result = effect()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("cordis effect failed while deactivating %s", self.plugin_id)
        self.effects.clear()
        self.state = FiberState.PENDING
        self._done.clear()


def _invoke_plugin(plugin: Any, ctx: PluginContext, config: Any, fiber: Fiber | None = None) -> Any:
    target = plugin
    if isinstance(plugin, type):
        # Instantiate class plugins so normal ``def apply(self, ctx, config)``
        # receives its bound self.  Static/classmethod apply also works on the
        # instance and constructors with required arguments can still expose a
        # class-level apply entry.
        try:
            target = plugin()
        except TypeError:
            target = plugin
    apply = getattr(target, "apply", None)
    setup = getattr(target, "setup", None)
    if setup is not None and fiber is not None:
        return setup(LegacyPluginContext(ctx._root, fiber), config)
    if apply is not None:
        try:
            return apply(ctx, config)
        except TypeError:
            return apply(ctx)
    if callable(target):
        try:
            return target(ctx, config)
        except TypeError:
            return target(ctx)
    raise TypeError(f"plugin {plugin!r} has no callable apply entry")


class Context:
    """Root service registry and reversible plugin runtime."""

    def __init__(self, *, parent: Context | None = None) -> None:
        self.parent = parent
        self._services: dict[str, Any] = {}
        self._owners: dict[str, Fiber] = {}
        self._records: list[_FiberRecord] = []
        self._events: dict[str, list[Callable[..., Any]]] = {}
        self._counter = 0
        self._closed = False
        self._reconcile_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def fibers(self) -> tuple[Fiber, ...]:
        return tuple(record.fiber for record in self._records)

    @property
    def events(self) -> Context:
        """Compatibility event facade; lifecycle events stay on this Context."""
        return self

    @property
    def services(self) -> MappingProxyType:
        return MappingProxyType(dict(self._services))

    def get(self, name: str, strict: bool = True) -> Any:
        if name in self._services:
            return self._services[name]
        if self.parent is not None:
            return self.parent.get(name, strict=False)
        if strict:
            raise KeyError(f"service {name!r} is not available")
        return None

    def has(self, name: str) -> bool:
        return self.get(name, strict=False) is not None

    def provide(self, name: str, value: Any, *, owner: Fiber | None = None) -> Cleanup:
        if not name:
            raise ValueError("service name must not be empty")
        if name in self._services:
            raise ValueError(f"service {name!r} is already provided")
        self._services[name] = value
        if owner is not None:
            self._owners[name] = owner
            owner.provided[name] = value
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed or self._services.get(name) is not value:
                return False
            disposed = True
            self._services.pop(name, None)
            self._owners.pop(name, None)
            if owner is not None:
                owner.provided.pop(name, None)
            self._schedule_reconcile()
            self.emit("service", name, None)
            return True

        self._schedule_reconcile()
        self.emit("service", name, value)
        return dispose

    def set(self, name: str, value: Any) -> None:
        if name not in self._services:
            raise KeyError(f"service {name!r} is not provided")
        self._services[name] = value

    def on(self, event: str, callback: Callable[..., Any]) -> Cleanup:
        self._events.setdefault(event, []).append(callback)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            listeners = self._events.get(event, [])
            try:
                listeners.remove(callback)
            except ValueError:
                return False
            return True

        return dispose

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        for callback in tuple(self._events.get(event, ())):
            result = callback(*args, **kwargs)
            if inspect.isawaitable(result):
                try:
                    asyncio.create_task(result)
                except RuntimeError:
                    pass

    async def filter(self, event: str, value: Any) -> Any:
        current = value
        for callback in tuple(self._events.get(event, ())):
            result = callback(current)
            if inspect.isawaitable(result):
                result = await result
            if result is not None and result is not False:
                current = result
        return current

    async def parallel(self, event: str, *args: Any, **kwargs: Any) -> None:
        results = []
        for callback in tuple(self._events.get(event, ())):
            result = callback(*args, **kwargs)
            results.append(result)
        await asyncio.gather(*(item for item in results if inspect.isawaitable(item)))

    async def serial(self, event: str, *args: Any, **kwargs: Any) -> Any:
        result = None
        for callback in tuple(self._events.get(event, ())):
            result = callback(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        return result

    async def waterfall(self, event: str, value: Any, *args: Any, **kwargs: Any) -> Any:
        current = value
        for callback in tuple(self._events.get(event, ())):
            result = callback(current, *args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                current = result
        return current

    def bail(self, event: str, *args: Any, **kwargs: Any) -> Any:
        for callback in tuple(self._events.get(event, ())):
            result = callback(*args, **kwargs)
            if result is not None and result is not False:
                return result
        return None

    def plugin(
        self,
        plugin: Any,
        config: Any = None,
        *,
        id: str | None = None,
        parent: Fiber | None = None,
    ) -> Fiber:
        if self._closed:
            raise RuntimeError("context is disposed")
        plugin_id = id or getattr(plugin, "id", None) or getattr(plugin, "name", None)
        if not plugin_id:
            plugin_id = getattr(plugin, "__name__", "plugin").lower()
        if any(item.fiber.plugin_id == plugin_id for item in self._records):
            raise ValueError(f"plugin {plugin_id!r} is already registered")
        fiber = Fiber(self, plugin, config, str(plugin_id), self._counter, parent)
        self._counter += 1
        self._records.append(_FiberRecord(fiber, fiber.order))
        self._schedule_reconcile()
        return fiber

    async def settle(self) -> tuple[Fiber, ...]:
        await self._reconcile()
        return self.fibers

    async def _reconcile(self) -> None:
        if self._closed:
            return
        async with self._lock:
            changed = True
            while changed:
                changed = False
                for record in self._records:
                    fiber = record.fiber
                    if fiber.state is FiberState.ACTIVE:
                        missing = tuple(
                            name for name in fiber.inject
                            if self.get(name, strict=False) is None
                        )
                        if missing:
                            fiber.missing = missing
                            await fiber.deactivate()
                            changed = True
                    if fiber.state is FiberState.PENDING:
                        before = fiber.state
                        await fiber.activate()
                        changed |= before is not fiber.state

    def _schedule_reconcile(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._reconcile_task is None or self._reconcile_task.done():
            self._reconcile_task = loop.create_task(self._reconcile())

    async def unload(self, plugin_id: str) -> bool:
        targets = [r.fiber for r in self._records if r.fiber.plugin_id == plugin_id]
        if not targets:
            return False
        for fiber in reversed(targets):
            await fiber.dispose()
            self._records = [r for r in self._records if r.fiber is not fiber]
        await self._reconcile()
        return True

    async def dispose(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        for record in reversed(self._records):
            try:
                await record.fiber.dispose()
            except Exception as exc:  # noqa: BLE001 - aggregate all cleanup failures
                errors.append(exc)
        self._records.clear()
        self._services.clear()
        self._owners.clear()
        self._events.clear()
        if errors:
            raise ExceptionGroup("cordis context cleanup failed", errors)

    async def close(self) -> None:
        await self.dispose()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        value = self.get(name, strict=False)
        if value is None:
            raise AttributeError(name)
        return value


__all__ = [
    "Cleanup",
    "Context",
    "Effect",
    "Fiber",
    "FiberState",
    "Inject",
    "PluginContext",
    "Service",
    "ServiceAccessError",
]
