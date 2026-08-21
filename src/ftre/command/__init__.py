"""Legacy Command package surface; implementations live in ``services``."""

from ftre.services.command import CommandManager, CommandService

__all__ = ["CommandManager", "CommandService"]
from .types import (
    CommandContext,
    CommandDef,
    CommandResult,
    Handled,
    Handler,
    Passthrough,
    RewritePrompt,
    SendMessage,
)

__all__ = [
    "CommandContext",
    "CommandDef",
    "CommandManager",
    "CommandResult",
    "Handled",
    "Handler",
    "Passthrough",
    "RewritePrompt",
    "SendMessage",
]
