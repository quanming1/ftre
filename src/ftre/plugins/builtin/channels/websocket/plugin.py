"""桌面 WebSocket 的协议组装层。

Channel 只负责连接和传输；Session、Inbox 等领域能力在本 Plugin 里组装成
通用的 attach baseline 和 control 回调。
"""

from __future__ import annotations

import logging
from typing import Any

from cordis import Context

from ftre.services.messaging.bus import SessionQueueFrame
from ftre.services.messaging.wire import SessionSubscribedFrame

from .channel import WebSocketChannel

logger = logging.getLogger(__name__)

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


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(part.get("text", ""))
            for part in value
            if isinstance(part, dict) and part.get("type", "text") == "text"
        )
    return str(value or "")


def _frame_payload(frame: Any) -> dict[str, Any]:
    return frame.model_dump(mode="json") if hasattr(frame, "model_dump") else dict(frame)


def apply(ctx: Context, config=None):
    """注册 WebSocket Channel 及其 Session 协议适配回调。"""
    options = config if isinstance(config, dict) else {}

    def current_inbox():
        return ctx.get("inbox", strict=False)

    async def queue_snapshot(session_id: str):
        inbox = current_inbox()
        if inbox is None:
            return None
        return await inbox.wire_snapshot(session_id)

    async def publish_snapshot(session_id: str) -> None:
        inbox = current_inbox()
        if inbox is None:
            return
        session = await ctx.sessions.get_session(session_id)
        channel_id = session["channel_id"] if session is not None else "ws"
        payload = await inbox.wire_snapshot(session_id)
        await ctx.message_bus.publish_frame(
            session_id,
            channel_id,
            SessionQueueFrame(session_id=session_id, payload=payload),
        )

    inbox = current_inbox()
    inbox_changed_spec = getattr(inbox, "changed_hook_spec", None)
    if inbox_changed_spec is not None:
        async def on_inbox_changed(payload, next_):
            await publish_snapshot(payload.session_id)
            return await next_()

        ctx.hook_runtime.register(
            inbox_changed_spec,
            on_inbox_changed,
            owner="websocket-channel",
            context=ctx,
            all_agent_scopes=True,
        )

    async def handle_control(frame_type: str, frame: dict, data: dict):
        """Session queue 操作的领域适配；Channel 不读取 Inbox。"""
        del frame
        if frame_type != "session.updateQueue":
            return {
                "ok": False,
                "error": {
                    "code": "invalid_control",
                    "message": "未知控制指令",
                    "session_id": str(data.get("session_id") or ""),
                    "retryable": False,
                },
            }
        session_id = str(data.get("session_id") or "")
        item_id = str(data.get("item_id") or "")
        action = data.get("action") or {}
        inbox = current_inbox()
        if not item_id or not isinstance(action, dict) or inbox is None:
            return {
                "ok": False,
                "error": {
                    "code": "inbox-unavailable",
                    "message": "队列能力不可用",
                    "session_id": session_id,
                    "retryable": False,
                },
            }
        kind = action.get("kind")
        try:
            snapshot = await inbox.snapshot(session_id)
            steering_ids = {
                item.request_id
                for item in snapshot.next_step
                if getattr(item, "source", "user") == "user"
            }
            if kind in {"edit", "remove"} and item_id in steering_ids:
                return {
                    "ok": False,
                    "error": {
                        "code": "steering-locked",
                        "message": "steering 消息已锁定，不能编辑或移除",
                        "session_id": session_id,
                        "retryable": False,
                    },
                }
            if kind == "edit":
                accepted = await inbox.edit(
                    session_id,
                    item_id,
                    _text_content(action.get("content")),
                    action.get("attachments"),
                )
            elif kind == "remove":
                accepted = await inbox.remove(session_id, item_id)
            elif kind == "steer":
                if not any(item.request_id == item_id for item in snapshot.next_turn):
                    return {
                        "ok": False,
                        "error": {
                            "code": "steer-not-available",
                            "message": "只有 queued 消息可以提升为 steering",
                            "session_id": session_id,
                            "retryable": False,
                        },
                    }
                accepted = await inbox.promote(session_id, item_id)
            else:
                return {
                    "ok": False,
                    "error": {
                        "code": "invalid_queue_action",
                        "message": "未知队列操作",
                        "session_id": session_id,
                        "retryable": False,
                    },
                }
        except Exception:  # noqa: BLE001 - map domain error to wire error
            return {
                "ok": False,
                "error": {
                    "code": "queue_update_failed",
                    "message": "队列操作失败",
                    "session_id": session_id,
                    "retryable": True,
                },
            }
        if not accepted:
            return {
                "ok": False,
                "error": {
                    "code": "item-not-pending",
                    "message": "消息已不在队列中",
                    "session_id": session_id,
                    "retryable": False,
                },
            }
        return {"ok": True, "value": await inbox.wire_snapshot(session_id)}

    async def baseline_provider(
        session_id: str,
        *,
        client_seq: Any = None,
    ):
        seq = -1
        events: list[dict[str, Any]] = []
        has_more = False
        resync_required = False
        try:
            seq_reader = getattr(ctx.sessions, "seq", None)
            if callable(seq_reader):
                value = seq_reader(session_id)
                seq = int(await value if hasattr(value, "__await__") else value)
            events_reader = getattr(ctx.sessions, "events_after", None)
            if callable(events_reader):
                after = int(client_seq) if isinstance(client_seq, (int, float)) else -1
                events, has_more, resync_required, seq = await events_reader(
                    session_id, after_seq=after, limit=500
                )
        except Exception:
            logger.debug("[websocket-channel] baseline session read failed", exc_info=True)
        status_reader = getattr(ctx.agents, "get_session_status", None)
        status = str(status_reader(session_id)) if callable(status_reader) else "idle"
        frames = [
            SessionSubscribedFrame(
                session_id=session_id,
                payload={
                    "seq": seq,
                    "events": events,
                    "status": status,
                    "has_more": has_more,
                    "resync_required": resync_required,
                },
            )
        ]
        queue = await queue_snapshot(session_id)
        if queue is not None:
            frames.append(SessionQueueFrame(session_id=session_id, payload=queue))
        return [_frame_payload(frame) for frame in frames]

    channel = WebSocketChannel(
        ctx.message_bus.bus,
        host=options.get("host", "127.0.0.1"),
        port=int(options.get("port", 48650)),
        attachment_service=ctx.attachments,
        http_service=ctx.http,
        control_handler=handle_control,
        baseline_provider=baseline_provider,
        snapshot_provider=queue_snapshot,
    )
    disposer = ctx.channels.register(channel, owner="websocket-channel")
    ctx.effect(lambda: disposer, label="channel:websocket")
    route_disposer = ctx.http.register_websocket_path(
        "/",
        "websocket-channel",
        channel._ws_endpoint,
    )
    ctx.effect(lambda: route_disposer, label="http:websocket")
