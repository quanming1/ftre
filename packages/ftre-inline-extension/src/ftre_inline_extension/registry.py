"""Handler registry is intentionally tiny; lifecycle belongs to the host Plugin."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from .parser import ExtensionParser
from .types import ExtensionContext, ExtensionRef, ExtensionResolution


class InlineExtensionHandler(Protocol):
    type: str

    def resolve(
        self, ref: ExtensionRef, *, context: ExtensionContext
    ) -> Awaitable[ExtensionResolution]: ...


@dataclass(frozen=True, slots=True)
class _Registration:
    handler: InlineExtensionHandler
    owner: str
    priority: int


@dataclass(frozen=True, slots=True)
class ExtensionDiagnostic:
    """当前注册表中可观察的 Handler 冲突。"""

    kind: str
    type: str
    owners: tuple[str, ...]
    winner: str


class InlineExtensionRegistry:
    """Parse references and dispatch them to one deterministic handler winner."""

    def __init__(self, parser: ExtensionParser | None = None) -> None:
        self.parser = parser or ExtensionParser()
        self._registrations: list[_Registration] = []

    @property
    def diagnostics(self) -> tuple[ExtensionDiagnostic, ...]:
        """返回按 type 聚合的冲突快照；disposer 后自动消失。"""
        grouped: dict[str, list[_Registration]] = {}
        for registration in self._registrations:
            grouped.setdefault(registration.handler.type, []).append(registration)
        diagnostics: list[ExtensionDiagnostic] = []
        for type_name, entries in sorted(grouped.items()):
            if len(entries) < 2:
                continue
            winner = min(entries, key=lambda item: (item.priority, item.owner))
            diagnostics.append(
                ExtensionDiagnostic(
                    kind="handler-conflict",
                    type=type_name,
                    owners=tuple(sorted(item.owner for item in entries)),
                    winner=winner.owner,
                )
            )
        return tuple(diagnostics)

    def register(
        self,
        handler: InlineExtensionHandler,
        *,
        owner: str,
        priority: int = 100,
    ) -> Callable[[], bool]:
        if not handler.type or not owner:
            raise ValueError("handler type and owner are required")
        registration = _Registration(handler, owner, priority)
        self._registrations.append(registration)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            try:
                self._registrations.remove(registration)
            except ValueError:
                return False
            return True

        return dispose

    def parse(self, text: str) -> tuple[ExtensionRef, ...]:
        return self.parser.parse(text)

    def handler_for(self, type_name: str) -> InlineExtensionHandler | None:
        candidates = [item for item in self._registrations if item.handler.type == type_name]
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item.priority, item.owner)).handler

    async def resolve(
        self,
        ref: ExtensionRef,
        *,
        context: ExtensionContext,
    ) -> ExtensionResolution:
        """选择 Handler 并解析一个引用；未知类型安全返回拒绝结果。"""
        handler = self.handler_for(ref.type)
        if handler is None:
            return ExtensionResolution(
                accepted=False,
                invocation_id="",
                reason=f"未注册扩展类型: {ref.type}",
            )
        return await handler.resolve(ref, context=context)


__all__ = ["ExtensionDiagnostic", "InlineExtensionHandler", "InlineExtensionRegistry"]
