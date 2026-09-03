"""Skill implementation for the generic inline extension protocol."""

from __future__ import annotations

import hashlib

from ftre_inline_extension import (
    ExtensionContext,
    ExtensionRef,
    ExtensionResolution,
)

from .service import SkillService


def _invocation_id(ref: ExtensionRef, context: ExtensionContext) -> str:
    value = "\0".join(
        (
            context.session_id,
            context.request_id,
            context.user_message_id,
            str(ref.span.start),
            ref.type,
            ref.name,
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _render_skill(name: str, content: str) -> str:
    return (
        "<skill_content>\n"
        f"<skill_name>{name}</skill_name>\n"
        "<skill_instructions>\n"
        f"{content.strip()}\n"
        "</skill_instructions>\n"
        "</skill_content>"
    )


class SkillInlineExtensionHandler:
    """Resolve a user-authored Skill reference without touching Agent internals."""

    type = "skill"

    def __init__(self, service: SkillService) -> None:
        self._service = service

    async def resolve(
        self,
        ref: ExtensionRef,
        *,
        context: ExtensionContext,
    ) -> ExtensionResolution:
        invocation_id = _invocation_id(ref, context)
        record = self._service.get(
            ref.name,
            context.agent_id or "default",
            context.workspace or None,
        )
        if record is None:
            return ExtensionResolution(
                accepted=False,
                invocation_id=invocation_id,
                reason=f"Skill 不存在: {ref.name}",
            )
        if record.disabled or not record.user_invocable:
            return ExtensionResolution(
                accepted=False,
                invocation_id=invocation_id,
                reason=f"Skill 不可由用户调用: {ref.name}",
            )
        return ExtensionResolution(
            accepted=True,
            invocation_id=invocation_id,
            display={
                "type": "skill",
                "name": record.name,
                "description": record.description,
                "source": record.source,
                "args": dict(ref.args),
            },
            message={
                "role": "user",
                "content": _render_skill(record.name, record.content),
                "metadata": {
                    "hide": True,
                    "source": "extension-invocation",
                    "extension_type": "skill",
                    "extension_name": record.name,
                    "extension_args": dict(ref.args),
                    "invocation_id": invocation_id,
                },
            },
        )


__all__ = ["SkillInlineExtensionHandler"]
