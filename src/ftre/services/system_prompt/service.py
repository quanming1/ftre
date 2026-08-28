"""System Prompt Service：有序、按 scope 的 system prompt 贡献注册表。

Prompt section 的生产者是 Plugin，AgentLoop 只在一次请求中调用 assemble；Service
不读写 Session 历史，也不把可变 section 列表暴露给模型。每次组装可生成 receipt，
用于诊断“哪些 prompt 被纳入了本轮”。
"""

from __future__ import annotations

import copy
import inspect
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from ftre_agent import AgentSubject

from .hooks import SYSTEM_PROMPT_ASSEMBLE_SPEC, PromptAssemblyPayload
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

    @staticmethod
    def _profile_value(profile: Any, attribute: str, source_name: str) -> str:
        value = getattr(profile, attribute, None) if profile is not None else None
        if value is None and profile is not None:
            sources = getattr(profile, "prompt_sources", None)
            if sources is not None:
                value = sources.get(source_name, "")
        return str(value or "").strip()

    @staticmethod
    def _profile_path(profile: Any) -> str:
        path = getattr(profile, "agent_dir", "") if profile is not None else ""
        if path:
            return str(path)
        trace = getattr(profile, "source_trace", ()) if profile is not None else ()
        return str(trace[0]) if trace else ""

    @staticmethod
    def _runtime_facts(*, channel_id: str, session_id: str, config: Any) -> str:
        lines = [
            "<FTRE_SYSTEM_FACT>",
            "<env>",
            f"channel_id={channel_id}",
            f"session_id={session_id}",
            f"os={os.name}",
            f"date={datetime.now(UTC).date().isoformat()}",
        ]
        if os.name == "nt":
            lines.append(
                "当前是 Windows 系统。书写路径时优先使用正斜杠 /；如果必须用反斜杠，"
                "在 JSON/字符串里务必写成双反斜杠 \\\\, 避免路径被转义。"
            )
        else:
            lines.append(
                "当前是类 Unix 系统（Linux/macOS）。路径使用正斜杠 /，优先使用绝对路径。"
            )
        if getattr(getattr(config, "llm", None), "vision", False):
            lines.append("vision=true：当前模型具备识图能力，可使用 read 工具读取图片和截图。")
        lines.extend(("</env>", "</FTRE_SYSTEM_FACT>"))
        return "\n".join(lines)

    def _builtin_sections(
        self,
        *,
        agent_id: str,
        session_id: str,
        profile: Any,
        channel_id: str,
        config: Any,
    ) -> list[PromptSection]:
        """Return Profile and runtime facts as normal Service-owned sections."""
        sections: list[PromptSection] = []
        profile_path = self._profile_path(profile)
        soul = self._profile_value(profile, "soul_prompt", "SOUL.md")
        if soul:
            soul_tag = (
                f'<SOUL desc="智能体人设：角色定义、语气、行为边界" '
                f'path="{profile_path}/SOUL.md">'
                if profile_path
                else "<SOUL>"
            )
            sections.append(
                PromptSection(
                    name="profile-soul",
                    content=f"{soul_tag}\n{soul}\n</SOUL>",
                    priority=10,
                    scope=f"agent:{agent_id}",
                    owner="agent_profile",
                    source="SOUL.md",
                )
            )
        user_prompt = self._profile_value(profile, "user_prompt_md", "USER.md")
        if user_prompt:
            user_tag = (
                f'<USER_PROFILE desc="用户偏好与个人要求" '
                f'path="{profile_path}/USER.md">'
                if profile_path
                else "<USER_PROFILE>"
            )
            sections.append(
                PromptSection(
                    name="profile-user",
                    content=f"{user_tag}\n{user_prompt}\n</USER_PROFILE>",
                    priority=11,
                    scope=f"agent:{agent_id}",
                    owner="agent_profile",
                    source="USER.md",
                )
            )
        if channel_id or config is not None:
            sections.append(
                PromptSection(
                    name="runtime-facts",
                    content=self._runtime_facts(
                        channel_id=channel_id,
                        session_id=session_id,
                        config=config,
                    ),
                    priority=20,
                    scope=f"session:{session_id}",
                    owner="system_prompt",
                    source="runtime",
                )
            )
        return sections

    def assemble(
        self,
        agent_id: str,
        session_id: str,
        workspace: str = "",
        messages: Iterable[Any] = (),
        *,
        base_prompt: str = "",
        profile: Any = None,
        channel_id: str = "",
        config: Any = None,
    ) -> str:
        """Build eligible sections in deterministic order and return rendered text."""
        return self.assemble_result(
            agent_id,
            session_id,
            workspace=workspace,
            messages=messages,
            base_prompt=base_prompt,
            profile=profile,
            channel_id=channel_id,
            config=config,
        ).text

    def assemble_result(
        self,
        agent_id: str,
        session_id: str,
        workspace: str = "",
        messages: Iterable[Any] = (),
        *,
        base_prompt: str = "",
        profile: Any = None,
        channel_id: str = "",
        config: Any = None,
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
        sections = [
            *self._builtin_sections(
                agent_id=agent_id,
                session_id=session_id,
                profile=profile,
                channel_id=channel_id,
                config=config,
            ),
            *self._sections,
        ]
        for section_order, section in enumerate(
            sorted(sections, key=lambda value: (value.priority, value.owner, value.name)),
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

    def receipt(
        self,
        agent_id: str,
        session_id: str,
        workspace: str = "",
        messages: Iterable[Any] = (),
        *,
        base_prompt: str = "",
        profile: Any = None,
        channel_id: str = "",
        config: Any = None,
    ) -> PromptAssemblyReceipt:
        """Assemble once and return the inclusion/error audit receipt."""
        assembly = self.assemble_result(
            agent_id,
            session_id,
            workspace,
            messages,
            base_prompt=base_prompt,
            profile=profile,
            channel_id=channel_id,
            config=config,
        )
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

    async def assemble_agent_prompt(
        self,
        *,
        agent_subject: AgentSubject,
        session_id: str,
        workspace: str,
        messages,
        base_prompt: str,
        inbound_data: dict[str, Any],
        config,
        hook_runtime,
        scope_context=None,
        event_loop=None,
        cancellation=None,
        profile: Any = None,
        channel_id: str = "",
    ) -> PromptAssembly:
        """组装结构化 Prompt 并在本域内 dispatch ``system-prompt/assemble``。

        该 Hook 的 Spec、Payload 与结果校验都由 system_prompt 域唯一拥有；
        Agent Runtime（ftre-agent-runtime）不 import 这些类型，只传入本轮
        上下文并消费最终 assembly（PRD-F33 §5.4 能力参数化）。

        ``hook_runtime`` 缺失时直接返回首次组装结果（与 waterfall default
        监听器等价）；``scope_context`` 由调用方用 HookRuntime 的
        ``context_for_scope`` 构造后传入。
        """
        assembly = self.assemble_result(
            agent_subject.agent_id,
            session_id,
            workspace=workspace,
            messages=messages,
            base_prompt=base_prompt,
            profile=profile,
            channel_id=channel_id,
            config=config,
        )
        if hook_runtime is None:
            return assembly
        payload = PromptAssemblyPayload(
            agent=agent_subject,
            session_id=session_id,
            workspace=workspace,
            assembly=assembly,
            messages=tuple(messages),
            inbound_data=inbound_data,
            config=copy.deepcopy(config),
            event_loop=event_loop,
            cancellation=cancellation,
            profile=profile,
            channel_id=channel_id,
        )
        result = await hook_runtime.dispatch(
            SYSTEM_PROMPT_ASSEMBLE_SPEC,
            payload,
            context=scope_context,
        )
        if not isinstance(result, PromptAssembly):
            raise TypeError("system-prompt/assemble must return PromptAssembly")
        return result

    def snapshot(self) -> tuple[PromptSection, ...]:
        """Return registered sections for diagnostics without exposing the mutable list."""
        return tuple(self._sections)
