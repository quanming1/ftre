"""
CommandManager — 指令注册 & 匹配。

支持两级指令：
- 系统级（system=True）：在 _dispatch 的 session lock 之外执行，
  用于需要立即响应的指令（如 /cancel），不受锁阻塞。
- 普通级（默认）：在 _step_command 中执行，受 session lock 保护，
  用于需要串行执行的指令（如 /compact）。

调用方只需：
    if await cmd.try_dispatch_system(data): return   # 锁外
    cmd_def = await cmd.try_dispatch(data)            # 锁内
    if cmd_def is not None: return                    # 匹配到，短路

内部自动判断 inbound.type、提取文本、前缀匹配，调用方无需关心细节。
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class CommandDef:
    """指令定义：命令面板渲染用元信息。"""
    command: str            # "/model"
    description: str        # "切换模型预设"
    args_hint: str = ""     # 参数提示，如 "[preset]"；空串 = 无参数
    system: bool = False    # 系统级指令：在 _dispatch 锁外执行，可立即响应


@dataclass
class CommandContext:
    """dispatch 匹配到指令后传给 handler 的上下文。"""
    raw: str                # 原始输入，如 "/model gpt-5"
    command: str            # 命中的指令，如 "/model"
    args: str | None        # 指令后的文本，如 "gpt-5"；无则为 None
    meta: dict[str, Any] = field(default_factory=dict)  # pipeline data，handler 可修改


Handler = Callable[[CommandContext], None | Awaitable[None]]
"""指令处理函数。通过 ctx.meta 回写结果（如 meta["result"]、meta["inbound"] 等）。

可以是同步函数，也可以是协程函数（async def）；dispatch 会统一 await。
"""


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
    ) -> "CommandManager":
        """注册一条指令。

        system=True → 系统级指令，在 _dispatch 的 session lock 之外执行，
        适合需要立即响应的指令（如 /cancel）。
        默认 system=False → 普通指令，在 _step_command 的 lock 内执行。

        按 command 长度降序排列，长的优先匹配。
        """
        entry = (CommandDef(command, description, args_hint, system), handler)
        if system:
            self._system_entries.append(entry)
            self._system_entries.sort(key=lambda e: -len(e[0].command))
        else:
            self._entries.append(entry)
            self._entries.sort(key=lambda e: -len(e[0].command))
        return self

    def list_commands(self) -> list[dict]:
        """返回已注册指令列表，供前端命令面板渲染。"""
        all_entries = self._system_entries + self._entries
        return [{"command": d.command, "description": d.description,
                 "args_hint": d.args_hint, "system": d.system}
                for d, _ in all_entries]

    # ─── 高级 API：接受 data dict，自动判断 & 派发 ─────────────

    def match(self, data: dict) -> CommandDef | None:
        """检查 data["inbound"] 是否匹配某个普通指令，但不执行。

        供调用方在执行前做前置工作（如持久化 user_message）。
        """
        text = self._extract_from_data(data)
        if text is None:
            return None
        matched = self._match_entry(self._entries, text)
        return matched[0] if matched else None

    async def try_dispatch_system(self, data: dict) -> bool:
        """尝试从 data["inbound"] 匹配并执行系统级指令。

        自动判断 inbound 类型、提取文本、前缀匹配。
        返回 True 表示命中并已执行，调用方应短路（return）。
        """
        return await self._try_dispatch_from(self._system_entries, data) is not None

    async def try_dispatch(self, data: dict) -> CommandDef | None:
        """尝试从 data["inbound"] 匹配并执行普通指令。

        自动判断 inbound 类型、提取文本、前缀匹配。
        返回匹配到的 CommandDef（已执行），未匹配返回 None。
        """
        return await self._try_dispatch_from(self._entries, data)

    # ─── 低级 API：直接传文本 ──────────────────────────────

    async def dispatch_system(self, raw: str | None, meta: dict[str, Any] | None = None) -> bool:
        """直接传文本匹配系统级指令。"""
        return await self._dispatch_from(self._system_entries, raw, meta) is not None

    async def dispatch(self, raw: str | None, meta: dict[str, Any] | None = None) -> bool:
        """直接传文本匹配普通指令。"""
        return await self._dispatch_from(self._entries, raw, meta) is not None

    # ─── 内部实现 ────────────────────────────────────────

    @staticmethod
    def _match_entry(
        entries: list[tuple[CommandDef, Handler]],
        raw: str,
    ) -> tuple[CommandDef, Handler, str | None] | None:
        """匹配文本，返回 (CommandDef, handler, args) 或 None。不执行 handler。"""
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

    def _extract_from_data(self, data: dict) -> str | None:
        """从 data["inbound"] 提取指令文本。

        仅当 inbound.type == "user_message" 且文本以 "/" 开头时返回文本，否则返回 None。
        """
        inbound = data.get("inbound")
        if inbound is None or inbound.type != "user_message":
            return None
        text = self._extract_text(inbound.data.get("content", ""))
        return text if text.startswith("/") else None

    async def _try_dispatch_from(
        self,
        entries: list[tuple[CommandDef, Handler]],
        data: dict,
    ) -> CommandDef | None:
        """从 data 提取文本，匹配 entries 中的指令并执行。

        返回匹配到的 CommandDef（已执行），未匹配返回 None。
        """
        text = self._extract_from_data(data)
        if text is None:
            return None
        cmd_def = await self._dispatch_from(entries, text, meta=data)
        if cmd_def is not None:
            logger.info(f"[command] 指令已处理 text={text!r} system={entries is self._system_entries}")
        return cmd_def

    async def _dispatch_from(
        self,
        entries: list[tuple[CommandDef, Handler]],
        raw: str | None,
        meta: dict[str, Any] | None,
    ) -> CommandDef | None:
        """从指定列表匹配并派发指令。返回匹配到的 CommandDef，未匹配返回 None。

        handler 通过 ctx.meta 回写结果，不需要返回值。
        handler 可同步可异步（async def），异步 handler 会被 await。
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
            await result
        return d
