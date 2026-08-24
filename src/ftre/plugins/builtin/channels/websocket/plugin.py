"""桌面 WebSocket Channel 的可选 Provider。

它把 WebSocket 协议适配到公开的 Bus/Channel/Attachment Service；连接集合和
FastAPI 路由由 WebSocketChannel 自己拥有，卸载时必须一并停止。
"""

from __future__ import annotations

from cordis import Context

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
    projection = getattr(ctx.sessions, "projection", None)

    def current_inbox():
        # Inbox may be restarted independently; resolving through Context keeps
        # this protocol provider from retaining a disposed Service instance.
        return ctx.get("inbox", strict=False)

    async def publish_snapshot(session_id: str) -> None:
        inbox = current_inbox()
        if inbox is None:
            return
        session = await ctx.sessions.get_session(session_id)
        channel_id = session["channel_id"] if session is not None else "ws"
        await ctx.message_bus.bus.publish_outbound(
            _session_frame(
                "session/queue",
                session_id,
                channel_id,
                await inbox.wire_snapshot(session_id),
            )
        )

    async def publish_status(session_id: str, status: str) -> None:
        session = await ctx.sessions.get_session(session_id)
        channel_id = session["channel_id"] if session is not None else "ws"
        await ctx.message_bus.bus.publish_outbound(
            _session_frame(
                "session/status",
                session_id,
                channel_id,
                {"session_id": session_id, "status": status},
            )
        )

    # Inbox emits these facts after each durable mutation.  Listening to the
    # Hook rather than binding callbacks to one Inbox instance makes restart
    # and unload safe: the listener resolves the current Service each time.
    inbox = current_inbox()
    inbox_changed_spec = getattr(inbox, "changed_hook_spec", None)
    inbox_status_spec = getattr(inbox, "status_hook_spec", None)

    if inbox_changed_spec is not None:
        async def on_inbox_changed(payload, next_):
            await publish_snapshot(payload.session_id)
            return await next_()

        async def on_inbox_status(payload, next_):
            await publish_status(payload.session_id, payload.status)
            return await next_()

        for spec, callback, label in (
            (inbox_changed_spec, on_inbox_changed, "channel:ws:inbox-changed"),
            (inbox_status_spec, on_inbox_status, "channel:ws:inbox-status"),
        ):
            if spec is None:
                continue
            receipt = ctx.hook_runtime.register(
                spec,
                callback,
                owner="websocket-channel",
                context=ctx,
                all_agent_scopes=True,
            )
            # HookRuntime 已绑定当前 Plugin Fiber；不再重复登记 receipt disposer。
            del receipt, label

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
        session_projection=projection,
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


def _session_frame(kind: str, session_id: str, channel_id: str, data):
    """Build the existing Bus envelope without coupling the plugin to Gateway."""
    from ftre.services.messaging.bus import BusMessage

    return BusMessage(
        type=kind,
        from_channel=channel_id,
        to_channel=channel_id,
        from_session=session_id,
        to_session=session_id,
        data=data,
    )
