from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any

from .receipt import PromptAssemblyReceipt
from .types import PromptSection


class SystemPromptService:
    key = "system_prompt"

    def __init__(self) -> None:
        self._sections: list[PromptSection] = []
        self._sequence = 0
        self._last_receipt: PromptAssemblyReceipt | None = None

    def register_section(self, section: PromptSection, owner: str | None = None, scope: str | None = None):
        if owner or scope:
            section = PromptSection(**{**section.__dict__, "owner": owner or section.owner, "scope": scope or section.scope})
        self._sections.append(section)
        self._sequence += 1
        sequence = self._sequence
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            try:
                self._sections.remove(section)
            except ValueError:
                return False
            return True

        return dispose

    def _eligible(self, section: PromptSection, agent_id: str, session_id: str) -> bool:
        return section.scope == "global" or section.scope == f"agent:{agent_id}" or section.scope == f"session:{session_id}"

    def assemble(self, agent_id: str, session_id: str, workspace: str = "", messages: Iterable[Any] = ()) -> str:
        parts: list[str] = []
        records: list[dict[str, Any]] = []
        for order, section in enumerate(sorted(self._sections, key=lambda value: (value.priority, value.owner, value.name)), start=1):
            if not self._eligible(section, agent_id, session_id):
                continue
            try:
                content = section.content or (section.factory({"agent_id": agent_id, "session_id": session_id, "workspace": workspace}) if section.factory else "")
                if inspect.isawaitable(content):
                    raise TypeError("Prompt factory must be synchronous")
                content = str(content)
                if content:
                    parts.append(content)
                records.append({"name": section.name, "owner": section.owner, "source": section.source, "scope": section.scope, "order": order, "bytes": len(content.encode()), "token_estimate": max(1, len(content) // 4) if content else 0, "included": bool(content), "error": None})
            except Exception as exc:
                records.append({"name": section.name, "owner": section.owner, "source": section.source, "scope": section.scope, "order": order, "bytes": 0, "token_estimate": 0, "included": False, "error": str(exc)})
                if section.required:
                    raise
        text = "\n\n".join(parts)
        self._last_receipt = PromptAssemblyReceipt(agent_id, session_id, tuple(records), len(text.encode()), max(1, len(text) // 4) if text else 0)
        return text

    def receipt(self, agent_id: str, session_id: str, workspace: str = "", messages: Iterable[Any] = ()) -> PromptAssemblyReceipt:
        self.assemble(agent_id, session_id, workspace, messages)
        assert self._last_receipt is not None
        return self._last_receipt

    def snapshot(self) -> tuple[PromptSection, ...]:
        return tuple(self._sections)

