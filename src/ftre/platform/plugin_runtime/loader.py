"""Official cordis-py backed plugin loader.

The project runtime owns manifest selection and diagnostics only. Plugin
activation, dependency epochs, effects, and teardown are delegated to the
installed ``cordis-py`` Fiber/Registry implementation; this module must not
recreate a second lifecycle state machine.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any

from cordis import Context, Fiber, FiberState

from .diagnostics import PluginStartupError, PluginStatus
from .discovery import PluginDiscovery
from .manifest import PluginManifest


class _ManifestEntry:
    """Name one manifest instance without changing the official Cordis API."""

    def __init__(self, plugin: Any, plugin_id: str) -> None:
        self._plugin = plugin
        self.name = plugin_id
        self.inject = getattr(plugin, "inject", ()) or ()
        config = getattr(plugin, "Config", None)
        if config is not None:
            self.Config = config

    def __call__(self, ctx: Context, config: Any = None) -> Any:
        """Invoke ftre's accepted entry shapes through official Fiber loading."""
        target = self._plugin
        if isinstance(target, type):
            try:
                target = target()
            except TypeError:
                try:
                    target = target(ctx, config)
                except TypeError:
                    # Let the final callable/apply validation report the exact
                    # unsupported class shape through the Fiber diagnostics.
                    pass
        apply = getattr(target, "apply", None)
        if callable(apply):
            return _call_entry(apply, ctx, config)
        if callable(target):
            return _call_entry(target, ctx, config)
        raise TypeError(f"plugin {self.name!r} has no callable apply entry")


def _call_entry(entry: Callable[..., Any], ctx: Context, config: Any) -> Any:
    """Support the documented two-argument entry and a one-argument entry."""
    try:
        signature = inspect.signature(entry)
    except (TypeError, ValueError):
        return entry(ctx, config)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    accepts_varargs = any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    if accepts_varargs or len(positional) >= 2:
        return entry(ctx, config)
    return entry(ctx)


async def _await_maybe(awaitable: Any) -> None:
    """Await a cordis cleanup handle when the operation is asynchronous."""
    if inspect.isawaitable(awaitable):
        await awaitable


class PluginLoader:
    """Translate selected manifests into official cordis-py Fibers."""

    def __init__(self, context: Context, *, discovery: PluginDiscovery | None = None) -> None:
        self.context = context
        self.discovery = discovery or PluginDiscovery()
        self._fibers: dict[str, Fiber] = {}
        self._entries: dict[str, _ManifestEntry] = {}
        self._manifests: dict[str, PluginManifest] = {}
        self._errors: dict[str, BaseException] = {}
        self._started_at: dict[str, float] = {}
        self.restart_required = False

    async def load(self, manifests: list[PluginManifest]) -> tuple[PluginStatus, ...]:
        """Resolve, register, await and enforce required-plugin startup policy."""
        for manifest in manifests:
            if manifest.id in self._manifests:
                raise ValueError(f"plugin {manifest.id!r} is already loaded")
            self._manifests[manifest.id] = manifest
            self._started_at[manifest.id] = time.perf_counter()
            try:
                entry = self.discovery.resolve(manifest)
                plugin_config = manifest.config
                validate = getattr(entry, "validate_config", None)
                if callable(validate):
                    plugin_config = validate(plugin_config)
                named_entry = _ManifestEntry(entry, manifest.id)
                fiber = self.context.plugin(named_entry, plugin_config)
                self._entries[manifest.id] = named_entry
                self._fibers[manifest.id] = fiber
            except Exception as exc:  # noqa: BLE001 - retain import/setup diagnostics
                self._errors[manifest.id] = exc

        # Official cordis has no ftre-specific ``Context.settle``. Awaiting
        # each Fiber drains its own loading inertia while dependency epochs
        # remain owned by ReflectService/Fiber.
        await self._await_fibers()
        statuses = self.statuses()
        required_failures = [
            status
            for status in statuses
            if status.required and status.state not in {FiberState.ACTIVE, "ACTIVE"}
        ]
        if required_failures:
            await _await_maybe(self.context.dispose())
            raise PluginStartupError("required plugin startup failed", statuses)
        return statuses

    async def _await_fibers(self) -> None:
        """Await all currently registered fibers and retain their failures."""
        for plugin_id, fiber in self._fibers.items():
            try:
                await fiber.await_()
            except BaseException as exc:  # noqa: BLE001 - status owns failure reporting
                self._errors.setdefault(plugin_id, exc)

    async def unload(self, plugin_id: str) -> bool:
        """Dispose one official Fiber and flag immutable host surfaces."""
        fiber = self._fibers.pop(plugin_id, None)
        self._entries.pop(plugin_id, None)
        self._manifests.pop(plugin_id, None)
        self._errors.pop(plugin_id, None)
        self._started_at.pop(plugin_id, None)
        if fiber is None:
            return False
        await _await_maybe(fiber.dispose())
        self.restart_required = self.restart_required or plugin_id.startswith(("http", "websocket"))
        return True

    async def restart(self, plugin_id: str) -> bool:
        """Restart one existing Fiber using official cordis-py lifecycle."""
        fiber = self._fibers.get(plugin_id)
        if fiber is None:
            return False
        self._errors.pop(plugin_id, None)
        try:
            await fiber.restart()
        except BaseException as exc:  # noqa: BLE001 - expose through diagnostics
            self._errors[plugin_id] = exc
        return fiber.state is FiberState.ACTIVE

    async def dispose(self) -> None:
        """Dispose the root Context and forget loader-owned handles."""
        await _await_maybe(self.context.dispose())
        self._fibers.clear()
        self._entries.clear()
        self._manifests.clear()
        self._errors.clear()
        self._started_at.clear()

    def _missing(self, fiber: Fiber | None) -> tuple[str, ...]:
        """Derive unresolved dependencies from official Fiber declarations."""
        if fiber is None:
            return ()
        missing: list[str] = []
        for name in fiber.inject:
            if fiber.ctx.get(name, strict=False) is None:
                missing.append(str(name))
        return tuple(missing)

    def statuses(self) -> tuple[PluginStatus, ...]:
        """Project official Fiber state into stable ftre diagnostics."""
        result: list[PluginStatus] = []
        for plugin_id, manifest in self._manifests.items():
            fiber = self._fibers.get(plugin_id)
            error = self._errors.get(plugin_id)
            state: FiberState | str = fiber.state if fiber else FiberState.FAILED
            if error is None and state is FiberState.FAILED:
                error_code = "apply_failed"
            elif error is not None:
                error_code = "entry_import_failed" if fiber is None else "apply_failed"
            else:
                error_code = None
            result.append(
                PluginStatus(
                    id=plugin_id,
                    source=manifest.source,
                    entry=manifest.entry_text,
                    state=state,
                    required=manifest.required,
                    error=str(error) if error else None,
                    error_code=error_code,
                    missing=self._missing(fiber),
                    restart_required=self.restart_required,
                    duration_ms=(time.perf_counter() - self._started_at[plugin_id]) * 1000,
                )
            )
        return tuple(result)
