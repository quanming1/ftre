"""Public command registry facade used by CLI and Agent integrations."""

from __future__ import annotations

from typing import Any

from .manager import CommandManager


class CommandService:
    """Keep command ownership and unregister callbacks behind one Service key."""
    key = "commands"

    def __init__(self, manager: CommandManager | None = None) -> None:
        self.manager = manager or CommandManager()

    def register(self, command: str, handler, **kwargs: Any):
        self.manager.register(command, handler, **kwargs)
        return lambda: self.manager.unregister(command)

    def register_def(self, command_def: Any):
        self.manager.register_def(command_def)
        return lambda: self.manager.unregister(command_def.command)

    def dispatch(self, *args: Any, **kwargs: Any):
        return self.manager.try_dispatch(*args, **kwargs)

    def list(self) -> list[dict[str, Any]]:
        return self.manager.list_commands()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.manager, name)
