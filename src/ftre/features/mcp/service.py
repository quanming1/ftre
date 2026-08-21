"""Feature-owned MCP server state and connection scope registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class McpServerState:
    name: str
    scope: str
    connected: bool
    owner: str


class McpService:
    """Track global/private MCP servers without coupling to a transport adapter."""
    key = "mcp"

    def __init__(self, connection_manager=None) -> None:
        self._servers: dict[tuple[str, str], McpServerState] = {}
        self._connections: dict[tuple[str, str], Any] = {}
        self.connection_manager = connection_manager

    async def start_and_register(self, raw_config: dict[str, Any]) -> None:
        """Start the Feature-owned MCP connection pool for the raw config."""
        if self.connection_manager is not None:
            await self.connection_manager.start_and_register(raw_config)

    async def reload_and_register(self, raw_config: dict[str, Any], source: str = "feature") -> None:
        """Reload the Feature-owned connection pool under its own lock."""
        if self.connection_manager is not None:
            await self.connection_manager.reload_and_register(raw_config, source=source)

    async def stop(self) -> None:
        """Stop connections and watchers before the Feature Fiber is disposed."""
        if self.connection_manager is not None:
            await self.connection_manager.stop()

    def register_server(self, name: str, config: dict[str, Any], scope: str = "global", owner: str = "mcp"):
        """Reserve a scoped server name and return a disposer for Plugin unload."""
        key = (scope, name)
        if key in self._servers:
            raise ValueError(f"MCP server {name!r} already registered in {scope}")
        state = McpServerState(name, scope, False, owner)
        self._servers[key] = state

        def dispose() -> bool:
            return self._servers.pop(key, None) is not None

        return dispose

    def list(self, scope: str | None = None) -> tuple[McpServerState, ...]:
        """List registered MCP servers, optionally limited to global/private scope."""
        return tuple(state for state in self._servers.values() if scope is None or state.scope == scope)

    def is_connected(self, name: str, scope: str = "global") -> bool:
        """Report connection state without exposing transport objects."""
        state = self._servers.get((scope, name))
        return bool(state and state.connected)

    async def connect(self, name: str, scope: str = "global") -> bool:
        """Mark a declared server connected; transport adapters own the socket."""
        state = self._servers.get((scope, name))
        if state is None:
            raise KeyError(name)
        self._servers[(scope, name)] = McpServerState(state.name, state.scope, True, state.owner)
        return True

    async def disconnect(self, name: str, scope: str = "global") -> bool:
        """Clear state and drop the scoped connection cache."""
        state = self._servers.get((scope, name))
        if state is None:
            return False
        self._servers[(scope, name)] = McpServerState(state.name, state.scope, False, state.owner)
        self._connections.pop((scope, name), None)
        return True
