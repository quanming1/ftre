"""Plugin instance lifecycle and reverse-order effect cleanup."""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, Any

from .context import Cleanup, FtreContext, run_cleanup
from .events import INTERNAL_PLUGIN_STATUS

if TYPE_CHECKING:
    from .registry import Plugin, PluginRegistry

logger = logging.getLogger(__name__)


class PluginState(str, Enum):
    PENDING = "PENDING"
    LOADING = "LOADING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    UNLOADING = "UNLOADING"
    DISPOSED = "DISPOSED"


class PluginInstance:
    """One configured plugin instance bound to a service scope."""

    def __init__(
        self,
        *,
        runtime_id: str,
        plugin: Plugin,
        scope: FtreContext,
        config: Any,
        registry: PluginRegistry,
    ) -> None:
        self.runtime_id = runtime_id
        self.plugin = plugin
        self.name = plugin.name
        self.scope = scope
        self.config = config
        self.registry = registry
        self.inject = tuple(dict.fromkeys(plugin.inject))
        self.provided_services = type(plugin).provided_services()
        allowed = set(self.inject).union(self.provided_services)
        self.context = scope.bind(self, allowed)
        self.state = PluginState.PENDING
        self.error: BaseException | None = None
        self._effects: list[Cleanup] = []

    @property
    def is_pending(self) -> bool:
        return self.state is PluginState.PENDING

    @property
    def is_active(self) -> bool:
        return self.state is PluginState.ACTIVE

    def add_effect(self, cleanup: Cleanup) -> None:
        if self.state in {PluginState.UNLOADING, PluginState.DISPOSED}:
            raise RuntimeError(
                f"plugin {self.name!r} cannot register effects while unloading"
            )
        self._effects.append(cleanup)

    async def activate(self) -> None:
        if self.state is not PluginState.PENDING:
            return
        self._transition(PluginState.LOADING)
        try:
            result = self.plugin.setup(self.context, self.config)
            if hasattr(result, "__await__"):
                result = await result
            if result is not None:
                self.context.effect(result)
            missing = [
                name
                for name in self.provided_services
                if self.scope._lookup(name) is _MISSING
            ]
            if missing:
                raise RuntimeError(
                    f"plugin {self.name!r} declared but did not provide services: {missing}"
                )
        except BaseException as exc:
            self.error = exc
            await self._cleanup_effects()
            self._transition(PluginState.FAILED)
            logger.exception("[plugin] setup failed: %s", self.name, exc_info=exc)
            return
        self.error = None
        self._transition(PluginState.ACTIVE)
        logger.info("[plugin] ACTIVE: %s v%s", self.name, self.plugin.version)

    async def deactivate(self) -> None:
        if self.state is PluginState.PENDING:
            return
        if self.state is PluginState.DISPOSED:
            return
        self._transition(PluginState.UNLOADING)
        await self._cleanup_effects()
        self._transition(PluginState.PENDING)

    async def dispose(self) -> None:
        if self.state is PluginState.DISPOSED:
            return
        if self.state is not PluginState.PENDING:
            self._transition(PluginState.UNLOADING)
            await self._cleanup_effects()
        self._transition(PluginState.DISPOSED)
        logger.info("[plugin] DISPOSED: %s", self.name)

    async def _cleanup_effects(self) -> None:
        while self._effects:
            cleanup = self._effects.pop()
            try:
                await run_cleanup(cleanup)
            except Exception:
                logger.exception("[plugin] cleanup failed: %s", self.name)

    def _transition(self, new_state: PluginState) -> None:
        old_state = self.state
        if old_state is new_state:
            return
        self.state = new_state
        self.scope.events.emit(INTERNAL_PLUGIN_STATUS, self, old_state, new_state)


from .context import _MISSING
