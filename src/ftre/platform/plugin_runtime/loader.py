"""Cordis-backed plugin loader."""

from __future__ import annotations

import time

from cordis import Context, Fiber, FiberState

from .diagnostics import PluginStartupError, PluginStatus
from .discovery import PluginDiscovery
from .manifest import PluginManifest


class PluginLoader:
    def __init__(self, context: Context, *, discovery: PluginDiscovery | None = None) -> None:
        self.context = context
        self.discovery = discovery or PluginDiscovery()
        self._fibers: dict[str, Fiber] = {}
        self._manifests: dict[str, PluginManifest] = {}
        self._started_at: dict[str, float] = {}
        self.restart_required = False

    async def load(self, manifests: list[PluginManifest]) -> tuple[PluginStatus, ...]:
        for manifest in manifests:
            if manifest.id in self._fibers:
                raise ValueError(f"plugin {manifest.id!r} is already loaded")
            self._manifests[manifest.id] = manifest
            self._started_at[manifest.id] = time.perf_counter()
            try:
                entry = self.discovery.resolve(manifest)
                plugin_config = manifest.config
                validate = getattr(entry, "validate_config", None)
                if callable(validate):
                    plugin_config = validate(plugin_config)
                fiber = self.context.plugin(entry, plugin_config, id=manifest.id)
                self._fibers[manifest.id] = fiber
            except Exception as exc:  # noqa: BLE001 - import/setup diagnostics must be retained
                # Keep an observable failed record even if import itself failed.
                self._fibers[manifest.id] = _failed_fiber(self.context, manifest.id, exc, "entry_import_failed")
        await self.context.settle()
        statuses = self.statuses()
        required_failures = [
            status for status in statuses
            if status.required and status.state not in {FiberState.ACTIVE, "ACTIVE"}
        ]
        if required_failures:
            await self.context.dispose()
            raise PluginStartupError("required plugin startup failed", statuses)
        return statuses

    async def unload(self, plugin_id: str) -> bool:
        result = await self.context.unload(plugin_id)
        self._fibers.pop(plugin_id, None)
        self._manifests.pop(plugin_id, None)
        if result:
            self.restart_required = self.restart_required or plugin_id.startswith(("http", "websocket"))
        return result

    async def dispose(self) -> None:
        await self.context.dispose()
        self._fibers.clear()

    def statuses(self) -> tuple[PluginStatus, ...]:
        result: list[PluginStatus] = []
        for plugin_id, manifest in self._manifests.items():
            fiber = self._fibers.get(plugin_id)
            state = fiber.state if fiber else FiberState.FAILED
            error = str(fiber.error) if fiber and fiber.error else None
            error_code = getattr(fiber, "error_code", None) if fiber else None
            if error_code is None and state is FiberState.FAILED:
                error_code = "apply_failed"
            result.append(
                PluginStatus(
                    id=plugin_id,
                    source=manifest.source,
                    entry=manifest.entry_text,
                    state=state,
                    required=manifest.required,
                    error=error,
                    error_code=error_code,
                    missing=fiber.missing if fiber else (),
                    restart_required=self.restart_required,
                    duration_ms=(time.perf_counter() - self._started_at[plugin_id]) * 1000,
                )
            )
        return tuple(result)


def _failed_fiber(context: Context, plugin_id: str, error: BaseException, error_code: str) -> Fiber:
    # A real Fiber is preferable to a second ad-hoc state machine in diagnostics.
    fiber = Fiber(context, lambda _ctx: None, {}, plugin_id, len(context.fibers))
    fiber.error = error
    fiber.error_code = error_code
    fiber.state = FiberState.FAILED
    return fiber
