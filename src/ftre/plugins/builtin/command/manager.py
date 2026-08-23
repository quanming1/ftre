"""CommandRuntime：注册、匹配和执行命令。

Runtime 只负责 Command Plane。它不会创建 Turn，也不会解释 Agent 数据面结果。
它是 CommandService 的内部实现，不是 AgentLoop；命令 handler 只能通过显式
``CommandContext`` 消费公开 Service。生命周期事件写入 Session metadata，方便
诊断，但命令正文不会自动进入聊天历史。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

from .types import CommandContext, CommandDef, CommandResult, Handler

logger = logging.getLogger(__name__)


class CommandRuntime:
    """命令注册与执行的唯一 Owner。"""

    def __init__(
        self,
        lifecycle: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self._system_entries: list[tuple[CommandDef, Handler]] = []
        self._entries: list[tuple[CommandDef, Handler]] = []
        self._lifecycle = lifecycle
        self._sequence = 0
        self._request_results: dict[str, CommandResult] = {}

    def register(
        self,
        command: str,
        handler: Handler,
        *,
        description: str = "",
        args_hint: str = "",
        system: bool = False,
        persist_input: bool = True,
        source: str = "builtin",
        sub_commands: list[CommandDef] | None = None,
    ) -> Callable[[], bool]:
        """注册命令并返回可逆、幂等的注销函数。"""
        definition = CommandDef(
            command=command,
            description=description,
            args_hint=args_hint,
            system=system,
            persist_input=persist_input,
            source=source,
            sub_commands=sub_commands or [],
            handler=handler,
        )
        self.register_def(definition)
        return lambda: self.unregister(command)

    def register_def(self, definition: CommandDef) -> Callable[[], bool]:
        if definition.handler is None:
            raise ValueError(f"CommandDef.handler is None for {definition.command!r}")
        entries = self._system_entries if definition.system else self._entries
        if any(item.command == definition.command for item, _ in entries):
            raise ValueError(f"command already registered: {definition.command}")
        entries.append((definition, definition.handler))
        entries.sort(key=lambda item: -len(item[0].command))
        return lambda: self.unregister(definition.command)

    def unregister(self, command: str) -> bool:
        for entries in (self._system_entries, self._entries):
            for index, (definition, _) in enumerate(entries):
                if definition.command == command:
                    entries.pop(index)
                    logger.info("[command] unregistered %s", command)
                    return True
        return False

    def list_commands(self) -> list[dict[str, Any]]:
        entries = self._system_entries + self._entries
        return [
            {
                "command": definition.command,
                "description": definition.description,
                "args_hint": definition.args_hint,
                "system": definition.system,
                "source": definition.source,
            }
            for definition, _ in entries
        ]

    def text_from(self, data: Any) -> str | None:
        inbound = data.get("inbound") if isinstance(data, dict) else getattr(data, "inbound", None)
        if inbound is None or inbound.type != "user_message":
            return None
        content = inbound.data.get("content", "")
        if isinstance(content, list):
            content = next(
                (
                    segment.get("text") or segment.get("data") or ""
                    for segment in content
                    if isinstance(segment, dict) and segment.get("type") == "text"
                ),
                "",
            )
        if not isinstance(content, str) or not content.startswith("/"):
            return None
        return content

    def parse(self, data: Any) -> CommandDef | None:
        raw = self.text_from(data)
        if raw is None:
            return None
        matched = self._match(self._system_entries + self._entries, raw)
        return matched[0] if matched else None

    def match(self, data: Any) -> CommandDef | None:
        raw = self.text_from(data)
        if raw is None:
            return None
        matched = self._match(self._entries, raw)
        return matched[0] if matched else None

    def match_any(self, data: Any) -> CommandDef | None:
        return self.parse(data)

    async def dispatch_inbound(
        self,
        inbound: Any,
        *,
        system: bool = False,
        definition: CommandDef | None = None,
    ) -> CommandResult | None:
        raw = self.text_from({"inbound": inbound})
        if raw is None:
            return None
        entries = self._system_entries if system else self._entries
        matched = self._match(entries, raw)
        if matched is None:
            return None
        current, handler, args = matched
        if definition is not None and current is not definition:
            raise ValueError("parsed command definition does not match inbound command")
        return await self._execute(current, handler, raw, args, inbound)

    async def dispatch(
        self,
        raw: str | None,
        *,
        inbound: Any,
        system: bool = False,
    ) -> CommandResult | None:
        if not raw:
            return None
        entries = self._system_entries if system else self._entries
        matched = self._match(entries, raw)
        if matched is None:
            return None
        definition, handler, args = matched
        return await self._execute(definition, handler, raw, args, inbound)

    async def _execute(
        self,
        definition: CommandDef,
        handler: Handler,
        raw: str,
        args: str | None,
        inbound: Any,
    ) -> CommandResult:
        """按定义执行一次命令并记录成对的 command/run、command/done 事件。"""
        request_id = inbound.metadata.request_id
        if request_id and request_id in self._request_results:
            return self._request_results[request_id]
        self._sequence += 1
        command_id = request_id or f"command_{self._sequence}"
        await self._emit("command/run", {
            "command_id": command_id,
            "name": definition.command,
            "args": args if definition.persist_input else None,
            "source": {"kind": "user"},
            "session_id": inbound.data.get("session_id") or inbound.from_session,
            "request_id": inbound.metadata.request_id,
        })
        context = CommandContext(
            raw=raw,
            command=definition.command,
            args=args,
            inbound=inbound,
        )
        try:
            result = handler(context)
            if inspect.isawaitable(result):
                result = await result
            normalized = self._normalize_result(definition.command, result)
        except asyncio.CancelledError:
            await self._emit("command/done", {
                "command_id": command_id,
                "kind": "error",
                "text": "command cancelled",
                "session_id": inbound.data.get("session_id") or inbound.from_session,
                "request_id": inbound.metadata.request_id,
            })
            raise
        except Exception as exc:
            await self._emit("command/done", {
                "command_id": command_id,
                "kind": "error",
                "text": str(exc),
                "session_id": inbound.data.get("session_id") or inbound.from_session,
                "request_id": inbound.metadata.request_id,
            })
            raise
        await self._emit("command/done", {
            "command_id": command_id,
            "kind": normalized.kind,
            "text": normalized.text,
            "source_event_seq": normalized.source_event_seq,
            "session_id": inbound.data.get("session_id") or inbound.from_session,
            "request_id": inbound.metadata.request_id,
        })
        if request_id and normalized.kind == "success":
            self._request_results[request_id] = normalized
        return normalized

    @staticmethod
    def _normalize_result(command: str, value: Any) -> CommandResult:
        if value is None:
            return CommandResult.success()
        if not isinstance(value, CommandResult):
            raise TypeError(f"command {command!r} handler must return CommandResult")
        if value.kind not in {"success", "error"}:
            raise TypeError(f"command {command!r} returned invalid result kind")
        if value.kind == "error" and not value.text.strip():
            raise ValueError(f"command {command!r} error text must not be empty")
        return value

    @staticmethod
    def _match(
        entries: list[tuple[CommandDef, Handler]],
        raw: str,
    ) -> tuple[CommandDef, Handler, str | None] | None:
        command_line = raw.strip()
        for definition, handler in entries:
            if command_line == definition.command or command_line.startswith(definition.command + " "):
                args = command_line[len(definition.command):].strip() or None
                return definition, handler, args
        return None

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        callback = self._lifecycle
        if callback is None:
            return
        result = callback(event_type, payload)
        if inspect.isawaitable(result):
            await result

__all__ = ["CommandRuntime"]
