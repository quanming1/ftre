"""Public Command Service facade."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .manager import CommandRuntime
from .types import CommandDef, CommandResult, Handler


class CommandService:
    """Command Plane 的公开 Service；不暴露内部注册表或 Agent runtime。"""

    key = "commands"

    def __init__(self, runtime: CommandRuntime | None = None) -> None:
        self.runtime = runtime or CommandRuntime()

    def register(self, command: str, handler: Handler, **kwargs: Any) -> Callable[[], bool]:
        return self.runtime.register(command, handler, **kwargs)

    def register_def(self, command_def: CommandDef) -> Callable[[], bool]:
        return self.runtime.register_def(command_def)

    def parse(self, data: Any) -> CommandDef | None:
        return self.runtime.parse(data)

    def is_command_input(self, data: Any) -> bool:
        """Return whether an inbound user message is slash-command shaped."""
        return self.runtime.text_from(data) is not None

    def match(self, data: Any) -> CommandDef | None:
        return self.runtime.match(data)

    def match_any(self, data: Any) -> CommandDef | None:
        return self.runtime.match_any(data)

    async def dispatch_inbound(
        self,
        inbound: Any,
        *,
        system: bool = False,
        definition: CommandDef | None = None,
    ) -> CommandResult | None:
        return await self.runtime.dispatch_inbound(
            inbound,
            system=system,
            definition=definition,
        )

    def bind_lifecycle(self, callback):
        return self.runtime.bind_lifecycle(callback)

    def list(self) -> list[dict[str, Any]]:
        return self.runtime.list_commands()


__all__ = ["CommandService"]
