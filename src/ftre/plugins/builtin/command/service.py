"""Command Service：接入层指令的解析、注册和执行边界。

Command 是接入协议，不是 Agent 的聊天消息。Service 把 slash command 解析成
结构化定义并交给 ``CommandRuntime``；普通命令在控制面完成，不进入
LLM 上下文，Feature 只需要依赖这个公开 key。
"""
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
        """注册一个命令并返回可逆 disposer，供 Provider 绑定到 Fiber。"""
        return self.runtime.register(command, handler, **kwargs)

    def register_def(self, command_def: CommandDef) -> Callable[[], bool]:
        """注册已构造的命令定义并返回可逆 disposer。"""
        return self.runtime.register_def(command_def)

    def parse(self, data: Any) -> CommandDef | None:
        """从接入数据解析 slash command 定义，不执行它。"""
        return self.runtime.parse(data)

    def is_command_input(self, data: Any) -> bool:
        """Return whether an inbound user message is slash-command shaped."""
        return self.runtime.text_from(data) is not None

    def match(self, data: Any) -> CommandDef | None:
        """匹配普通用户命令；未命中返回 None。"""
        return self.runtime.match(data)

    def match_any(self, data: Any) -> CommandDef | None:
        """匹配普通或 system 命令，供内部控制面使用。"""
        return self.runtime.match_any(data)

    async def dispatch_inbound(
        self,
        inbound: Any,
        *,
        system: bool = False,
        definition: CommandDef | None = None,
    ) -> CommandResult | None:
        """在接入边界执行一个结构化命令并返回统一结果。"""
        return await self.runtime.dispatch_inbound(
            inbound,
            system=system,
            definition=definition,
        )

    def bind_lifecycle(self, callback):
        """绑定命令生命周期观察者，并返回幂等解绑函数。"""
        return self.runtime.bind_lifecycle(callback)

    def list(self) -> list[dict[str, Any]]:
        """返回命令定义摘要，不暴露 handler 对象。"""
        return self.runtime.list_commands()


__all__ = ["CommandService"]
