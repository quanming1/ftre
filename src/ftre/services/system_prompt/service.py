"""System Prompt Service：有序、按 scope 的 system prompt 贡献注册表。

Prompt section 的生产者是 Plugin，AgentLoop 只在一次请求中调用 assemble；Service
不读写 Session 历史，也不把可变 section 列表暴露给模型。每次组装可生成 receipt，
用于诊断“哪些 prompt 被纳入了本轮”。
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any

from .receipt import PromptAssemblyReceipt
from .types import PromptAssembly, PromptContribution, PromptSection


class SystemPromptService:
    """组装可见 prompt section，并为每次请求保留审计 receipt。"""
    key = "system_prompt"

    def __init__(self) -> None:
        self._sections: list[PromptSection] = []
        self._sequence = 0

    def register_section(self, section: PromptSection, owner: str | None = None, scope: str | None = None):
        """Register a section and return a disposer used by its owning Plugin."""
        if owner or scope:
            section = PromptSection(**{**section.__dict__, "owner": owner or section.owner, "scope": scope or section.scope})
        self._sections.append(section)
        self._sequence += 1
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
        """Build eligible sections in deterministic order and return rendered text."""
        return self.assemble_result(agent_id, session_id, workspace=workspace).text

    def assemble_result(
        self,
        agent_id: str,
        session_id: str,
        workspace: str = "",
        messages: Iterable[Any] = (),
        *,
        base_prompt: str = "",
    ) -> PromptAssembly:
        """Render a structured, immutable assembly without touching message history."""
        del messages  # reserved for deterministic section factories in later slices
        contributions: list[PromptContribution] = []
        parts: list[str] = []
        order = 0
        base = (base_prompt or "").strip()
        if base:
            order += 1
            contributions.append(
                PromptContribution("config-base", base, "config", "config", "global", order)
            )
            parts.append(base)
        for section_order, section in enumerate(
            sorted(self._sections, key=lambda value: (value.priority, value.owner, value.name)),
            start=order + 1,
        ):
            if not self._eligible(section, agent_id, session_id):
                continue
            try:
                content = section.content or (section.factory({"agent_id": agent_id, "session_id": session_id, "workspace": workspace}) if section.factory else "")
                if inspect.isawaitable(content):
                    raise TypeError("Prompt factory must be synchronous")
                content = str(content)
                if content:
                    parts.append(content)
                contributions.append(
                    PromptContribution(
                        section.name,
                        content,
                        section.owner,
                        section.source,
                        section.scope,
                        section_order,
                    )
                )
            except Exception as exc:
                if section.required:
                    raise
                contributions.append(
                    PromptContribution(
                        section.name,
                        f"[section failed: {type(exc).__name__}]",
                        section.owner,
                        section.source,
                        section.scope,
                        section_order,
                    )
                )
        return PromptAssembly(
            agent_id=agent_id,
            session_id=session_id,
            workspace=workspace,
            contributions=tuple(contributions),
            text="\n\n".join(parts),
        )

    def receipt(self, agent_id: str, session_id: str, workspace: str = "", messages: Iterable[Any] = ()) -> PromptAssemblyReceipt:
        """Assemble once and return the inclusion/error audit receipt."""
        assembly = self.assemble_result(agent_id, session_id, workspace, messages)
        records = tuple(
            {
                "name": item.name,
                "owner": item.owner,
                "source": item.source,
                "scope": item.scope,
                "order": item.order,
                "bytes": len(item.content.encode()),
                "token_estimate": max(1, len(item.content) // 4)
                if item.content
                else 0,
                "included": bool(item.content),
                "error": None,
            }
            for item in assembly.contributions
        )
        return PromptAssemblyReceipt(
            agent_id,
            session_id,
            records,
            len(assembly.text.encode()),
            max(1, len(assembly.text) // 4) if assembly.text else 0,
        )

    def snapshot(self) -> tuple[PromptSection, ...]:
        """Return registered sections for diagnostics without exposing the mutable list."""
        return tuple(self._sections)
