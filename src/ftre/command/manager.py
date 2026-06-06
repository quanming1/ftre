"""
CommandManager — 指令注册 & 匹配。

Handler 按需修改 ctx.meta（如 /cancel 替换 inbound）。
dispatch 返回是否命中了一条指令。

用法::

    cmd = CommandManager()
    cmd.register("/help", lambda ctx: ctx.meta.update(result="帮助信息"))
    cmd.register("/cancel", lambda ctx: ...)  # 修改 ctx.meta["inbound"]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class CommandDef:
    """指令定义：命令面板渲染用元信息。"""
    command: str            # "/model"
    description: str        # "切换模型预设"
    args_hint: str = ""     # 参数提示，如 "[preset]"；空串 = 无参数


@dataclass
class CommandContext:
    """dispatch 匹配到指令后传给 handler 的上下文。"""
    raw: str                # 原始输入，如 "/model gpt-5"
    command: str            # 命中的指令，如 "/model"
    args: str | None        # 指令后的文本，如 "gpt-5"；无则为 None
    meta: dict[str, Any] = field(default_factory=dict)  # pipeline data，handler 可修改


Handler = Callable[[CommandContext], None]
"""指令处理函数。通过 ctx.meta 回写结果（如 meta["result"]、meta["inbound"] 等）。"""


class CommandManager:
    """指令注册 & 前缀匹配 & 派发。"""

    def __init__(self) -> None:
        self._entries: list[tuple[CommandDef, Handler]] = []

    def register(
        self,
        command: str,
        handler: Handler,
        *,
        description: str = "",
        args_hint: str = "",
    ) -> "CommandManager":
        """注册一条指令。按 command 长度降序，长的优先匹配。"""
        self._entries.append((CommandDef(command, description, args_hint), handler))
        self._entries.sort(key=lambda e: -len(e[0].command))
        return self

    def list_commands(self) -> list[dict]:
        """返回已注册指令列表，供前端命令面板渲染。"""
        return [{"command": d.command, "description": d.description,
                 "args_hint": d.args_hint} for d, _ in self._entries]

    def dispatch(self, raw: str | None, meta: dict[str, Any] | None = None) -> bool:
        """匹配指令并调用 handler。返回 True 表示命中。

        handler 通过 ctx.meta 回写结果，不需要返回值。
        """
        if not raw:
            return False
        cmd = raw.strip()
        for d, handler in self._entries:
            if cmd == d.command or cmd.startswith(d.command + " "):
                args = cmd[len(d.command):].strip() or None
                ctx = CommandContext(raw=raw, command=d.command, args=args,
                                     meta=meta or {})
                handler(ctx)
                return True
        return False
