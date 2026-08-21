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

    def __init__(self) -> None:
        self._servers: dict[tuple[str, str], McpServerState] = {}
        self._connections: dict[tuple[str, str], Any] = {}

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
