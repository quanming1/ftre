"""Command Service 的 Provider Plugin。

它注入 ``sessions``；AgentService 在命令真正执行时由当前 Composition 解析，
避免 Command Provider 与 Agent Provider 形成循环依赖。命令注册和生命周期事件
监听都通过 ``ctx.effect`` 绑定，卸载插件时不会残留命令处理器。
"""

from __future__ import annotations

from cordis import Context

from ftre.services.messaging.bus import (
    MESSAGING_INBOUND_SPEC,
    BusMessage,
    CommandMessagePayload,
    IngressResult,
    SessionCommandMessage,
)

from .builtin import register_builtin_commands
from .service import CommandService
from .types import CommandResult

provide = ("commands",)
inject = ("sessions", "http", "hook_runtime", "message_bus", "agents")


def apply(ctx: Context, config=None):
    """发布 CommandService，并把内置命令注册到当前 Fiber 的生命周期中。"""
    async def persist_command_event(event_type, payload):
        session_id = payload.get("session_id") or ""
        if not session_id:
            return
        await ctx.sessions.append_command_event(
            session_id,
            {"type": event_type, **payload},
        )

    service = ctx.get("commands", strict=False)
    if service is None:
        service = CommandService(lifecycle=persist_command_event)
        ctx.provide("commands", service)

    disposers = register_builtin_commands(
        service.runtime,
        agents=ctx.agents,
        sessions=ctx.sessions,
    )
    for index, disposer in enumerate(disposers):
        ctx.effect(lambda disposer=disposer: disposer, label=f"command:builtin:{index}")

    from .router import build_router

    route_disposer = ctx.http.register_router(build_router(service), owner="commands")
    ctx.effect(lambda: route_disposer, label="http:commands")

    async def on_inbound(message: BusMessage, next_):
        """在进入 Inbox 前裁决 Command；普通输入继续交给后续 Listener。"""
        if message.type == "turn_cancel":
            return await next_()
        definition = service.parse({"inbound": message})
        if definition is None:
            if not service.is_command_input({"inbound": message}):
                return await next_()
            result = CommandResult.error("命令不可用或未启用")
        elif getattr(definition, "system", False):
            await service.dispatch_inbound(message, definition=definition, system=True)
            return _accepted(message)
        else:
            result = await service.dispatch_inbound(message, definition=definition)
            if result is None:
                result = CommandResult.error("命令未执行")
        await _publish_result(ctx, message, result)
        return _accepted(message)

    receipt = ctx.hook_runtime.register(
        MESSAGING_INBOUND_SPEC,
        on_inbound,
        owner="commands",
        context=ctx,
        all_agent_scopes=True,
    )
    # HookRuntime 已绑定当前 Plugin Fiber；不为同一 receipt 增加第二个 Effect。
    del receipt


def _accepted(message: BusMessage) -> IngressResult:
    """Return the existing durable ingress ACK shape without opening a Turn."""
    return IngressResult(
        accepted=True,
        session_id=str(message.data.get("session_id") or message.from_session),
        request_id=str(message.metadata.request_id or message.id),
        created=True,
    )


async def _publish_result(ctx: Context, inbound: BusMessage, result) -> None:
    """Publish command text through the existing typed Session command envelope."""
    if result is None or not getattr(result, "text", ""):
        return
    level = "error" if getattr(result, "kind", "success") == "error" else "info"
    await ctx.message_bus.bus.publish_outbound(
        SessionCommandMessage(
            from_channel=inbound.from_channel,
            to_channel=inbound.from_channel,
            from_session=inbound.from_session,
            to_session=inbound.from_session,
            data=CommandMessagePayload(content=result.text, level=level),
            metadata=inbound.metadata,
        )
    )
