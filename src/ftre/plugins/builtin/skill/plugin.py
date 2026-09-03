"""Skill Plugin: catalog, CRUD routes, model tool and inline-message Handler."""

from __future__ import annotations

from cordis import Context
from ftre_agent import AGENT_BEFORE_REASONING_SPEC, BeforeReasoningResult
from ftre_inline_extension import ExtensionContext, InlineExtensionRegistry

from ftre.services.system_prompt.types import PromptSection

from .extension_handler import SkillInlineExtensionHandler
from .prompt import build_skill_prompt
from .service import SkillService
from .tool import build_load_skill_tool

inject = (
    "tools",
    "http",
    "config",
    "agent_profiles",
    "sessions",
    "hook_runtime",
    "system_prompt",
)
provide = ("skills", "inline_extensions")


async def apply(ctx: Context, config=None):
    """Publish Skill/extension services and bind every contribution to Plugin life."""
    existing = ctx.get("skills", strict=False)
    service = existing if isinstance(existing, SkillService) else None
    if service is None:
        service = SkillService(
            config_service=ctx.config,
            agent_profiles=ctx.agent_profiles,
        )
        ctx.provide("skills", service)

    registry = ctx.get("inline_extensions", strict=False)
    if not isinstance(registry, InlineExtensionRegistry):
        registry = InlineExtensionRegistry()
        ctx.provide("inline_extensions", registry)
    handler_disposer = registry.register(
        SkillInlineExtensionHandler(service),
        owner="skill",
    )
    ctx.effect(lambda: handler_disposer, label="inline-extension:skill")

    tool_disposer = ctx.tools.register(
        build_load_skill_tool(service), owner="skill", source="builtin"
    )
    ctx.effect(lambda: tool_disposer, label="tool:loadSkill")

    from .router import build_router

    route_disposer = ctx.http.register_router(build_router(service), owner="skill")
    ctx.effect(lambda: route_disposer, label="http:skill")

    prompt_disposer = ctx.system_prompt.register_section(
        PromptSection(
            name="skill-guidance",
            factory=build_skill_prompt(service),
            priority=50,
            owner="skill",
            source="builtin",
        )
    )
    ctx.effect(lambda: prompt_disposer, label="prompt:skill")

    if ctx.hook_runtime is not None and ctx.sessions is not None:
        async def on_before_reasoning(payload, next_):
            result = await next_()
            if payload.iteration != 1 or payload.cancellation.is_set():
                return result
            message = await _find_user_message(ctx.sessions, payload.session_id, payload.request_id)
            if message is None:
                return result
            refs = registry.parse(message.get_text_content() or "")
            if not refs:
                return result
            extensions = [
                {
                    "version": ref.version,
                    "type": ref.type,
                    "name": ref.name,
                    "args": dict(ref.args),
                    "raw": ref.raw,
                    "span": {"start": ref.span.start, "end": ref.span.end},
                }
                for ref in refs
            ]
            metadata = dict(message.metadata or {})
            metadata_changed = False
            if not isinstance(metadata.get("extensions"), list):
                metadata["extensions"] = extensions
                metadata_changed = True
            seen_invocations = {
                str(value)
                for value in metadata.get("extension_invocations", [])
                if isinstance(value, str)
            }

            session = await ctx.sessions.get_session(payload.session_id)
            session_metadata = await ctx.sessions.get_session_metadata(payload.session_id)
            agent_id = str(
                (session_metadata or {}).get("agent_id")
                or (session or {}).get("agent_id")
                or "default"
            )
            workspace = str((session or {}).get("workspace") or "")
            injected = []
            for ref in refs:
                resolution = await registry.resolve(
                    ref,
                    context=ExtensionContext(
                        session_id=payload.session_id,
                        agent_id=agent_id,
                        workspace=workspace,
                        user_message_id=message.id,
                        request_id=payload.request_id,
                        cancellation=payload.cancellation,
                    ),
                )
                if resolution.accepted and resolution.message is not None:
                    if resolution.invocation_id in seen_invocations:
                        continue
                    injected_message = dict(resolution.message)
                    injected_metadata = dict(injected_message.get("metadata") or {})
                    injected_metadata["invocation_id"] = resolution.invocation_id
                    injected_message["metadata"] = injected_metadata
                    injected_message["id"] = (
                        f"extension_{resolution.invocation_id}"
                    )
                    injected.append(injected_message)
                    await ctx.sessions.upsert_message(
                        payload.session_id,
                        injected_message,
                    )
                    if resolution.invocation_id:
                        seen_invocations.add(resolution.invocation_id)
                        metadata_changed = True
            if metadata_changed:
                metadata["extension_invocations"] = sorted(seen_invocations)
                message.metadata = metadata
                await ctx.sessions.update_message(message)
            if not injected:
                return result
            return BeforeReasoningResult((*result.messages, *injected))

        receipt = ctx.hook_runtime.register(
            AGENT_BEFORE_REASONING_SPEC,
            on_before_reasoning,
            owner="skill-inline-extension",
            context=ctx,
            all_agent_scopes=True,
        )
        del receipt

    ctx.effect(service.clear_loaded, label="skills:session-loads")


async def _find_user_message(sessions, session_id: str, request_id: str):
    records = await sessions.get_messages_by_session(session_id)
    for record in reversed(records):
        message = sessions.record_to_msg(record)
        metadata = message.metadata or {}
        if metadata.get("request_id") == request_id and metadata.get("source", "user") == "user":
            return message
    return None
