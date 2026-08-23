"""接入层 Command Service、运行时和结构化结果模型。"""

from .manager import CommandRuntime
from .service import CommandService
from .types import CommandContext, CommandDef, CommandResult

__all__ = ["CommandContext", "CommandDef", "CommandResult", "CommandRuntime", "CommandService"]
