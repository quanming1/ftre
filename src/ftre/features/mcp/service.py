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
    key = "mcp"

    def __init__(self) -> None:
        self._servers: dict[tuple[str, str], McpServerState] = {}

    def register_server(self, name: str, config: dict[str, Any], scope: str = "global", owner: str = "mcp"):
        key = (scope, name)
        if key in self._servers:
            raise ValueError(f"MCP server {name!r} already registered in {scope}")
        state = McpServerState(name, scope, False, owner)
        self._servers[key] = state

        def dispose() -> bool:
            return self._servers.pop(key, None) is not None

        return dispose

    def list(self, scope: str | None = None) -> tuple[McpServerState, ...]:
        return tuple(state for state in self._servers.values() if scope is None or state.scope == scope)

    def is_connected(self, name: str, scope: str = "global") -> bool:
        state = self._servers.get((scope, name))
        return bool(state and state.connected)

