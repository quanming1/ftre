"""桌面 WebSocket Channel 的可选 Provider。

它把 WebSocket 协议适配到公开的 Bus/Channel/Attachment Service；连接集合和
FastAPI 路由由 WebSocketChannel 自己拥有，卸载时必须一并停止。
"""

from __future__ import annotations

from cordis import Context

from ftre.services.messaging.bus import SessionQueueFrame

from .channel import WebSocketChannel

inject = (
    "message_bus",
    "channels",
    "attachments",
    "sessions",
    "agents",
    "http",
    "hook_runtime",
)
provide = ()


def apply(ctx: Context, config=None):
    """创建并注册 WebSocket Channel，但不在 apply 中偷偷启动监听 Server。"""
    options = config if isinstance(config, dict) else {}

    def current_inbox():
        # 这里是有意保留的动态解析例外：Inbox 可独立 restart，WebSocket Channel
        # 必须继续存活并切换到新 Service，不能把已 dispose 的实例捕获进闭包。
        # 这不是缺失依赖的静默 fallback；Composition 已把 Inbox 声明为 required，
        # 缺失时启动门禁失败，运行中实例消失时操作返回 inbox-unavailable。
        return ctx.get("inbox", strict=False)

    async def publish_snapshot(session_id: str) -> None:
        inbox = current_inbox()
        if inbox is None:
            return
        session = await ctx.sessions.get_session(session_id)
        channel_id = session["channel_id"] if session is not None else "ws"
        queue_snapshot = await inbox.wire_snapshot(session_id)
        await ctx.message_bus.publish_frame(
            session_id,
            channel_id,
            SessionQueueFrame(session_id=session_id, payload=queue_snapshot),
        )

    # Inbox emits these facts after each durable mutation. Listening to the Hook
    # rather than binding callbacks to one Inbox instance makes restart safe.
    # queue 快照经 publish_frame(session/queue) 下发；blocked 状态走
    # SessionLog 的 session/status 事件（自动透传），status Hook 不发帧。
    inbox = current_inbox()
    inbox_changed_spec = getattr(inbox, "changed_hook_spec", None)

    if inbox_changed_spec is not None:
        async def on_inbox_changed(payload, next_):
            await publish_snapshot(payload.session_id)
            return await next_()

        receipt = ctx.hook_runtime.register(
            inbox_changed_spec,
            on_inbox_changed,
            owner="websocket-channel",
            context=ctx,
            all_agent_scopes=True,
        )
        # HookRuntime 已绑定当前 Plugin Fiber；不再重复登记 receipt disposer。
        del receipt

    def status_provider(session_id: str) -> str:
        inbox = current_inbox()
        queue_status = inbox.status(session_id) if inbox is not None else None
        return queue_status or ctx.agents.status(session_id)

    channel = WebSocketChannel(
        ctx.message_bus.bus,
        host=options.get("host", "127.0.0.1"),
        port=int(options.get("port", 48650)),
        attachment_service=ctx.attachments,
        http_service=ctx.http,
        sessions_service=ctx.sessions,
        inbox_provider=current_inbox,
        status_provider=status_provider,
    )
    disposer = ctx.channels.register(channel, owner="websocket-channel")
    ctx.effect(lambda: disposer, label="channel:websocket")
    route_disposer = ctx.http.register_websocket_path(
        "/",
        "websocket-channel",
        channel._ws_endpoint,
    )
    ctx.effect(lambda: route_disposer, label="http:websocket")
