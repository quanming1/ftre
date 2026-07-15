"""CommandManager — 指令注册 & 匹配 & 派发。

支持两级指令：
- 系统级（system=True）：在 _dispatch 的 session lock 之外执行，
  用于需要立即响应的指令（如 /cancel），不受锁阻塞。
- 普通级（默认）：在 _step_command 中执行，受 session lock 保护，
  用于需要串行执行的指令（如 /compact）。

调用方只需：
    if await cmd.try_dispatch_system(data): return   # 锁外
    result = await cmd.try_dispatch(data)             # 锁内
    if result is not None:                            # 匹配到
        match result:
            case SubmitPrompt(...): ...  # 继续 pipeline
            case Handled(): ...          # 短路

内部自动判断 inbound.type、提取文本、前缀匹配，调用方无需关心细节。
"""
from __future__ import annotations

import inspect
import logging
from typing import Any

from ftre.command.types import (
    CommandContext,
    CommandDef,
    CommandResult,
    Handled,
    Handler,
)

logger = logging.getLogger(__name__)


class CommandManager:
    """指令注册 & 前缀匹配 & 派发。

    两级指令：
    - 系统级（system=True）：try_dispatch_system() 匹配，在 session lock 外执行
    - 普通级：try_dispatch() 匹配，在 session lock 内执行
    """

    def __init__(self) -> None:
        self._system_entries: list[tuple[CommandDef, Handler]] = []
        self._entries: list[tuple[CommandDef, Handler]] = []

    def register(
        self,
        command: str,
        handler: Handler,
        *,
        description: str = "",
        args_hint: str = "",
        system: bool = False,
        source: str = "builtin",
        sub_commands: list[CommandDef] | None = None,
    ) -> "CommandManager":
        """注册一条指令。

        system=True → 系统级指令，在 _dispatch 的 session lock 之外执行，
        适合需要立即响应的指令（如 /cancel）。
        默认 system=False → 普通指令，在 _step_command 的 lock 内执行。

        按 command 长度降序排列，长的优先匹配。
        """
        cmd_def = CommandDef(
            command=command,
            description=description,
            args_hint=args_hint,
            system=system,
            source=source,
            sub_commands=sub_commands or [],
        )
        entry = (cmd_def, handler)
        if system:
            self._system_entries.append(entry)
            self._system_entries.sort(key=lambda e: -len(e[0].command))
        else:
            self._entries.append(entry)
            self._entries.sort(key=lambda e: -len(e[0].command))
        return self

    def register_def(self, cmd_def: CommandDef) -> "CommandManager":
        """直接注册一个 CommandDef（handler 在 cmd_def.handler 上）。

        供 file_loader / skill_plugin 使用。
        """
        if cmd_def.handler is None:
            raise ValueError(f"CommandDef.handler is None for {cmd_def.command!r}")
        entry = (cmd_def, cmd_def.handler)
        if cmd_def.system:
            self._system_entries.append(entry)
            self._system_entries.sort(key=lambda e: -len(e[0].command))
        else:
            self._entries.append(entry)
            self._entries.sort(key=lambda e: -len(e[0].command))
        return self

    def unregister(self, command: str) -> bool:
        """注销一条指令。返回是否找到并删除。"""
        for entries in (self._system_entries, self._entries):
            for i, (d, _) in enumerate(entries):
                if d.command == command:
                    entries.pop(i)
                    logger.info(f"[command] 已注销指令 {command!r}")
                    return True
        return False

    def list_commands(self) -> list[dict]:
        """返回已注册指令列表，供前端命令面板渲染。"""
        all_entries = self._system_entries + self._entries
        return [{"command": d.command, "description": d.description,
                 "args_hint": d.args_hint, "system": d.system,
                 "source": d.source}
                for d, _ in all_entries]

    # ─── 高级 API：接受 data（PipelineData 或 dict），自动判断 & 派发 ────

    def match(self, data: Any) -> CommandDef | None:
        """检查 data["inbound"] 是否匹配某个普通指令，但不执行。

        供调用方在执行前做前置工作（如持久化 user_message）。
        """
        text = self._extract_from_data(data)
        if text is None:
            return None
        matched = self._match_entry(self._entries, text)
        return matched[0] if matched else None

    async def try_dispatch_system(self, data: Any) -> bool:
        """尝试从 data["inbound"] 匹配并执行系统级指令。

        自动判断 inbound 类型、提取文本、前缀匹配。
        返回 True 表示命中并已执行，调用方应短路（return）。
        """
        text = self._extract_from_data(data)
        if text is None:
            return False
        result = await self._dispatch_from(self._system_entries, text, meta=data)
        if result is not None:
            logger.info(f"[command] 系统指令已处理 text={text!r}")
            return True
        return False

    async def try_dispatch(self, data: Any) -> CommandResult | None:
        """尝试从 data["inbound"] 匹配并执行普通指令。

        返回 CommandResult（已执行），未匹配返回 None。
        """
        text = self._extract_from_data(data)
        if text is None:
            return None
        result = await self._dispatch_from(self._entries, text, meta=data)
        if result is not None:
            _, cmd_result = result
            logger.info(f"[command] 指令已处理 text={text!r}")
            return cmd_result
        return None

    # ─── 低级 API：直接传文本 ──────────────────────────────

    async def dispatch_system(self, raw: str | None, meta: dict[str, Any] | None = None) -> bool:
        """直接传文本匹配系统级指令。"""
        result = await self._dispatch_from(self._system_entries, raw, meta)
        return result is not None

    async def dispatch(self, raw: str | None, meta: dict[str, Any] | None = None) -> CommandResult | None:
        """直接传文本匹配普通指令。返回 CommandResult，未匹配返回 None。"""
        result = await self._dispatch_from(self._entries, raw, meta)
        return result[1] if result is not None else None

    # ─── 内部实现 ────────────────────────────────────────

    @staticmethod
    def _match_entry(
        entries: list[tuple[CommandDef, Handler]],
        raw: str,
    ) -> tuple[CommandDef, Handler, str | None] | None:
        """匹配文本，返回 (CommandDef, handler, args) 或 None。不执行 handler。

        子命令通过前缀匹配自然支持：
        注册 "/memory" 和 "/memory add" 两条指令，按长度降序排列，
        "/memory add key=val" 先匹配 "/memory add" → args="key=val"。
        不需要递归，靠最长前缀匹配即可。
        """
        cmd = raw.strip()
        if not cmd:
            return None
        for d, handler in entries:
            if cmd == d.command or cmd.startswith(d.command + " "):
                args = cmd[len(d.command):].strip() or None
                return (d, handler, args)
        return None

    @staticmethod
    def _extract_text(content) -> str:
        """从 user_message.content 抽取首段纯文本，兼容字符串与多模态分段数组。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for seg in content:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    return str(seg.get("text") or seg.get("data") or "")
        return ""

    def _extract_from_data(self, data) -> str | None:
        """从 data 提取指令文本。

        支持 PipelineData（dataclass）和 dict 两种格式。
        仅当 inbound.type == "user_message" 且文本以 "/" 开头时返回文本，否则返回 None。
        """
        if isinstance(data, dict):
            inbound = data.get("inbound")
        else:
            inbound = getattr(data, "inbound", None)
        if inbound is None or inbound.type != "user_message":
            return None
        text = self._extract_text(inbound.data.get("content", ""))
        return text if text.startswith("/") else None

    async def _dispatch_from(
        self,
        entries: list[tuple[CommandDef, Handler]],
        raw: str | None,
        meta: dict[str, Any] | None,
    ) -> tuple[CommandDef, CommandResult] | None:
        """从指定列表匹配并派发指令。返回 (CommandDef, CommandResult)，未匹配返回 None。

        handler 可同步可异步（async def），异步 handler 会被 await。
        handler 返回 None 视为 Handled（兼容旧 handler）。
        """
        if not raw:
            return None
        matched = self._match_entry(entries, raw)
        if matched is None:
            return None
        d, handler, args = matched
        # 注意：用 `meta if meta is not None else {}` 而非 `meta or {}`，
        # 否则传入空 dict（falsy）时会被换成新 dict，handler 对 meta 的
        # 修改（如 command_hit / inbound 替换）就回写不到调用方。
        ctx = CommandContext(raw=raw, command=d.command, args=args,
                             meta=meta if meta is not None else {})
        result = handler(ctx)
        if inspect.isawaitable(result):
            result = await result
        # None → Handled（兼容旧 handler）
        if result is None:
            result = Handled()
        return (d, result)
