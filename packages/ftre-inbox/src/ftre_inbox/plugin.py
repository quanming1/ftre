"""ftre-inbox 的 Cordis Provider Plugin。"""

from __future__ import annotations

from pathlib import Path

from cordis import Context

from ftre.services.messaging.bus import MESSAGING_INBOUND_SPEC

from .repository import InboxRepository
from .service import InboxService

# Inbox must be activated after Agent Runtime is available.  Cordis may settle
# independent Fibers in parallel; declaring this dependency prevents a race in
# which Inbox becomes ACTIVE before it can bind its admission handler to the
# AgentLoop, leaving the runtime on its ``inbox-unavailable`` fallback.
inject = ("sessions", "agents", "hook_runtime")
provide = ("inbox",)


async def apply(ctx: Context, config=None):
    """创建独立 Inbox；AgentService 只作为运行时执行依赖注入。"""
    existing = ctx.get("inbox", strict=False)
    if isinstance(existing, InboxService) and not existing.is_closed:
        return
    options = config if isinstance(config, dict) else {}
    # 插件拥有队列容量配置；核心 AgentConfig 不再解析 mailboxCapacity。
    agents_config = options.get("agents") if isinstance(options.get("agents"), dict) else {}
    context_config = (
        agents_config.get("context")
        if isinstance(agents_config.get("context"), dict)
        else {}
    )
    sessions = ctx.sessions
    root = options.get("inbox_dir")
    if root is None and sessions is not None and hasattr(sessions, "sessions_root"):
        root = Path(sessions.sessions_root()) / "_inbox"
    root = root or (Path.cwd() / ".ftre-inbox")
    exists = None
    request_seen = None
    if sessions is not None and hasattr(sessions, "has_session"):
        exists = sessions.has_session
    if sessions is not None and hasattr(sessions, "has_request_id"):
        request_seen = sessions.has_request_id
    legacy_root = sessions.sessions_root() if sessions is not None and hasattr(sessions, "sessions_root") else None
    try:
        capacity = max(
            1,
            int(
                options.get(
                    "capacity",
                    context_config.get(
                        "mailboxCapacity",
                        context_config.get("mailbox_capacity", 100),
                    ),
                )
            ),
        )
    except (TypeError, ValueError):
        capacity = 100
    repository = InboxRepository(
        root,
        capacity=capacity,
        session_exists=exists,
        request_seen=request_seen,
        legacy_root=legacy_root,
    )
    service = InboxService(
        repository,
        ctx.agents,
        hook_runtime=ctx.hook_runtime,
    )
    ctx.provide("inbox", service)

    async def on_inbound(message, next_):
        """接管未被 Command 消费的普通输入，并转换为 Queue admission。"""
        result = await next_()
        if result is not None:
            return result
        return await service.handle_bus_message(message)

    inbound_receipt = ctx.hook_runtime.register(
        MESSAGING_INBOUND_SPEC,
        on_inbound,
        owner="ftre-inbox",
        context=ctx,
        global_listener=True,
    )
    ctx.effect(lambda: inbound_receipt.dispose, label="inbox:messaging-inbound")
    await service.start()
    # SessionService emits the public disposed Hook; Inbox only reacts to that
    # fact and never replaces SessionService's lifecycle dispatcher.
    hook_runtime = ctx.hook_runtime
    if hook_runtime is not None:
        from ftre.services.agent.hooks import (
            AGENT_BEFORE_REASONING_SPEC,
            BeforeReasoningResult,
        )
        from ftre.services.session.hooks import SESSION_DISPOSED_SPEC

        async def on_before_reasoning(payload, next_):
            """把 active Turn 的 next-step 原子 claim 成 Core 普通消息。"""
            result = await next_()
            claimed = await service.claim_next_step_for_reasoning(payload.session_id)
            if not claimed:
                return result
            injected = tuple(
                {
                    "role": "user",
                    "content": item.content,
                }
                for item in claimed
            )
            return BeforeReasoningResult((*result.messages, *injected))

        before_reasoning_receipt = hook_runtime.register(
            AGENT_BEFORE_REASONING_SPEC,
            on_before_reasoning,
            owner="ftre-inbox",
            global_listener=True,
        )
        ctx.effect(
            lambda: before_reasoning_receipt.dispose,
            label="inbox:agent-before-reasoning",
        )

        async def on_session_disposed(payload):
            await service.delete_session(payload.session_id)

        receipt = hook_runtime.register(
            SESSION_DISPOSED_SPEC,
            on_session_disposed,
            owner="ftre-inbox",
            global_listener=True,
        )
        ctx.effect(lambda: receipt.dispose, label="inbox:session-disposed")
    ctx.effect(lambda: service.close, label="inbox:close")
