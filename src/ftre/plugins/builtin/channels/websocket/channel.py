"""WebSocket Channel：桌面客户端协议到 Bus 的适配层（PRD-F41 wire）。

连接 / session 模型：
- 一个客户端 = 一条物理 WebSocket，可 attach 多个 session（前端同时关注多会话）。
- session_id → set[WebSocket]；同一 session 允许多客户端 attach（多端同步）。

wire 帧（PRD-F41 §4.4，信封 {v, session_id, type, payload}）：
- 下行透传：BusMessage(type="downstream_frame") 的 data 即完整帧——
  Channel 只做 json.dumps + 按 attach 扇出 + per-session 输出锁保序。
- attach 基线三连（输出锁内）：session/subscribed{last_seq,status} →
  session/queue（若有 Inbox）→ 直播事件流。
- rpc 帧：prompt/cancel/updateQueue 的结算响应，对发起连接直回（不经 Bus 广播）。

Channel 只负责连接、帧校验、attach 和 outbound 推送；Session admission、命令解析
和 Agent 执行仍由 MessageBus/Inbox 完成。
"""
import asyncio
import base64
import binascii
import json
import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ftre.services.attachment import AttachmentService
from ftre.services.messaging.bus import (
    GLOBAL_SESSION,
    BusMessage,
    EventBus,
    InboundData,
    InboundMetadata,
)
from ftre.services.messaging.channel.base import Channel
from ftre.services.messaging.wire import (
    RpcFrame,
    SessionQueueFrame,
    SessionSubscribedFrame,
)

logger = logging.getLogger(__name__)


# ============================================================
# 附件校验（session.prompt.payload.attachments）
# ============================================================

ALLOWED_IMAGE_MIME = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024  # 3 MB
MAX_ATTACHMENTS_PER_MESSAGE = 8


def _prompt_text(value: Any) -> str:
    """将 prompt/updateQueue 的字符串或文本 parts 归一为内部文本。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(part.get("text", ""))
            for part in value
            if isinstance(part, dict) and part.get("type", "text") == "text"
        )
    return str(value or "")


def _validate_attachments(attachments) -> tuple[bool, str]:
    """校验 session.prompt.payload.attachments；返回 (ok, error)。"""
    if attachments is None:
        return True, ""
    if not isinstance(attachments, list):
        return False, "attachments 必须是数组"
    if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
        return False, f"附件数量超过上限 {MAX_ATTACHMENTS_PER_MESSAGE}"

    for i, att in enumerate(attachments):
        if not isinstance(att, dict):
            return False, f"attachments[{i}] 必须是对象"
        if att.get("type") != "image":
            return False, f"attachments[{i}].type 仅支持 'image'，收到 {att.get('type')!r}"
        mime = att.get("mime_type", "")
        if mime not in ALLOWED_IMAGE_MIME:
            return False, f"attachments[{i}].mime_type 不支持: {mime!r}"
        b64 = att.get("data")
        if not isinstance(b64, str) or not b64:
            return False, f"attachments[{i}].data 缺失或非字符串"
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return False, f"attachments[{i}].data 不是合法 base64"
        if len(raw) > MAX_ATTACHMENT_BYTES:
            limit_mb = MAX_ATTACHMENT_BYTES / 1024 / 1024
            actual_mb = len(raw) / 1024 / 1024
            return False, (
                f"attachments[{i}] 大小 {actual_mb:.2f}MB 超过上限 {limit_mb:.0f}MB"
            )
    return True, ""


def _persist_attachments(
    attachments: list | None,
    attachment_service: AttachmentService,
) -> None:
    """将 attachments 中的 base64 data 落盘，替换为 path（原地修改）。"""
    if not attachments:
        return
    for att in attachments:
        if not isinstance(att, dict) or att.get("type") != "image":
            continue
        b64 = att.get("data", "")
        mime = att.get("mime_type", "image/png")
        name = att.get("name", "")
        try:
            raw = base64.b64decode(b64)
        except Exception:  # noqa: BLE001 边界：附件落盘失败跳过
            logger.warning(f"[ws-channel] 附件落盘失败，跳过: {name}")
            continue
        path = attachment_service.save_image(raw, mime, original_name=name)
        del att["data"]
        att["path"] = path


class WebSocketChannel(Channel):
    """维护 WebSocket 连接集合，并把客户端帧转换为 BusMessage（PRD-F41 wire）。"""

    def __init__(
        self,
        bus: EventBus,
        host: str = "0.0.0.0",
        port: int = 48650,
        app: FastAPI | None = None,
        attachment_service: AttachmentService | None = None,
        http_service=None,
        sessions_service=None,
        inbox_provider=None,
        status_provider=None,
    ):
        super().__init__(channel_id="ws", name="WebSocket Channel", bus=bus)
        self.host = host
        self.port = port
        self.app = app
        if self.app is not None:
            from fastapi.middleware.cors import CORSMiddleware

            self.app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        # session_id → 关注该 session 的 ws 连接集合
        self._connections: dict[str, set[WebSocket]] = {}
        # 反向索引：ws → attach 过的 session_id（断开时清理）
        self._ws_sessions: dict[WebSocket, set[str]] = {}
        # per-session 输出锁：保证 attach 基线与实时帧的 FIFO 顺序
        self._session_output_locks: dict[str, asyncio.Lock] = {}
        # 能力注入（Plugin 构造时显式传入）
        self._sessions_service = sessions_service
        self._inbox_provider = inbox_provider
        self._status_provider = status_provider
        self._http_service = http_service
        self._server = None
        self._server_task: asyncio.Task | None = None
        self._attachment_service = attachment_service

        if self.app is not None:
            self._register_endpoint(self.app)

    def _register_endpoint(self, app: FastAPI) -> None:
        """Register this channel's endpoint exactly once on one app."""
        marker = "_ftre_ws_channel_registered"
        if getattr(app.state, marker, False):
            return
        app.websocket("/")(self._ws_endpoint)
        setattr(app.state, marker, True)

    def _current_inbox(self):
        provider = self._inbox_provider
        if callable(provider):
            return provider()
        return provider

    async def start(self) -> None:
        """启动 WebSocket 服务"""
        import uvicorn

        app = self._http_service.app if self._http_service is not None else self.app
        if app is None:
            raise RuntimeError("WebSocket Host App is not materialized")
        config = uvicorn.Config(
            app, host=self.host, port=self.port,
            log_level="warning", log_config=None,
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())
        logger.info(f"[ws-channel] listening on ws://{self.host}:{self.port}/")

    async def stop(self) -> None:
        """停止服务"""
        if self._server:
            self._server.should_exit = True
        if self._server_task:
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
        logger.info("[ws-channel] stopped")

    def _output_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_output_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_output_locks[session_id] = lock
        return lock

    # ============================================================
    # 下行：Bus downstream_frame → ws 透传
    # ============================================================

    async def send(self, msg: BusMessage) -> None:
        """Bus outbound → 推送给 attach 该 session 的 ws（零变换透传）。

        - downstream_frame：data 即完整 wire 帧，直接序列化扇出；
        - 全局广播（to_session == GLOBAL_SESSION）：扇出给所有活跃 ws。
        - per-session 输出锁保证 attach 基线与直播帧 FIFO。
        """
        frame_data = (
            msg.data.model_dump(mode="json")
            if hasattr(msg.data, "model_dump")
            else msg.data
        )
        text = json.dumps(frame_data, ensure_ascii=False, default=str)

        if msg.to_session == GLOBAL_SESSION:
            targets = list(self._ws_sessions.keys())
            if targets:
                await self._send_to_targets(targets, text)
            return

        async with self._output_lock(msg.to_session):
            targets = list(self._connections.get(msg.to_session, ()))
            if targets:
                await self._send_to_targets(targets, text)

    async def _send_to_targets(self, targets: list[WebSocket], text: str) -> None:
        """向目标 ws 列表发送文本帧，清理断开的连接。"""
        dead: list[WebSocket] = []
        for ws in targets:
            if ws.application_state != WebSocketState.CONNECTED:
                dead.append(ws)
                continue
            try:
                await ws.send_text(text)
            except Exception as e:  # noqa: BLE001 边界：发送失败清理连接
                logger.debug(f"[ws-channel] send 失败，准备关闭: {e}")
                dead.append(ws)

        for ws in dead:
            if ws.application_state != WebSocketState.DISCONNECTED:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001, S110 边界
                    pass

    # ============================================================
    # WebSocket 端点
    # ============================================================

    async def _ws_endpoint(self, ws: WebSocket) -> None:
        """WebSocket 连接入口"""
        await ws.accept()
        self._ws_sessions[ws] = set()
        logger.info("[ws-channel] connection established")

        try:
            while True:
                raw = await ws.receive_text()
                await self._on_message(raw, ws)
        except WebSocketDisconnect:
            pass
        except RuntimeError as e:
            msg_str = str(e)
            if "not connected" in msg_str:
                logger.debug(f"[ws-channel] connection closed by send failure: {e}")
            else:
                logger.warning(f"[ws-channel] connection error: {e}")
        except Exception as e:  # noqa: BLE001 边界
            logger.warning(f"[ws-channel] connection error: {e}")
        finally:
            attached = self._ws_sessions.get(ws, set())
            logger.info(f"[ws-channel] connection closed (sessions={list(attached)})")
            self._detach_all(ws)

    # ============================================================
    # 连接登记
    # ============================================================

    def _attach(self, session_id: str, ws: WebSocket) -> None:
        if not session_id:
            return
        self._connections.setdefault(session_id, set()).add(ws)
        self._ws_sessions.setdefault(ws, set()).add(session_id)

    def _detach(self, session_id: str, ws: WebSocket) -> None:
        conns = self._connections.get(session_id)
        if conns:
            conns.discard(ws)
            if not conns:
                self._connections.pop(session_id, None)
        sids = self._ws_sessions.get(ws)
        if sids:
            sids.discard(session_id)

    def _detach_all(self, ws: WebSocket) -> None:
        for sid in list(self._ws_sessions.get(ws, ())):
            self._detach(sid, ws)
        self._ws_sessions.pop(ws, None)

    # ============================================================
    # 上行帧处理（不变：F12 协议）
    # ============================================================

    async def _on_message(self, raw: str, ws: WebSocket) -> None:
        """收到客户端消息 → 通过 Bus 交给 Inbox/Command 边界。

        上行帧格式: {type, request_id, payload: {...}, metadata?}
        type: attach | detach | session.prompt | session.cancel | session.updateQueue
        """
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(frame, dict):
            return

        frame_type = frame.get("type", "")
        data = frame.get("payload") or {}
        if not isinstance(data, dict):
            return
        session_id = data.get("session_id", "")

        if frame_type == "attach":
            lock = self._output_lock(session_id)
            async with lock:
                self._attach(session_id, ws)
                await self._send_baseline(session_id, ws)
            return

        if frame_type == "detach":
            self._detach(session_id, ws)
            return

        if frame_type == "session.cancel":
            await self._on_cancel(ws, frame, data, session_id)
            return

        if frame_type == "session.updateQueue":
            await self._on_queue_update(ws, frame, data)
            return

        if frame_type != "session.prompt":
            logger.debug(f"[ws-channel] unknown frame type: {frame_type}")
            return

        await self._on_prompt(ws, frame, data, session_id)

    async def _on_prompt(self, ws, frame, data, session_id):
        """session.prompt：durable admission → rpc(ok+queue) / rpc(error)。"""
        if not session_id:
            logger.warning("[ws-channel] session.prompt 缺少 session_id，忽略")
            return
        request_id = frame.get("request_id") or ""
        if not isinstance(request_id, str) or not request_id:
            await self._send_rpc(ws, "", session_id, ok=False, error={
                "code": "missing_request_id",
                "message": "session.prompt 缺少 request_id，无法保证幂等接纳",
                "session_id": session_id,
                "retryable": False,
            })
            return

        mode = data.get("mode") or "queue"
        if mode not in {"queue", "steer"}:
            await self._send_rpc(ws, request_id, session_id, ok=False, error={
                "code": "invalid_mode",
                "message": "mode 只能是 queue 或 steer",
                "session_id": session_id,
                "retryable": False,
            })
            return

        ok, err = _validate_attachments(data.get("attachments"))
        if not ok:
            logger.warning(f"[ws-channel] session.prompt 附件非法: {err}")
            await self._send_rpc(ws, request_id, session_id, ok=False, error={
                "code": "invalid_attachments",
                "message": err,
                "session_id": session_id,
                "retryable": False,
            })
            return

        attachments = data.get("attachments")
        if attachments and self._attachment_service is None:
            await self._send_rpc(ws, request_id, session_id, ok=False, error={
                "code": "attachment_service_unavailable",
                "message": "附件服务未就绪，请稍后重试",
                "session_id": session_id,
                "retryable": True,
            })
            return
        if self._attachment_service is not None:
            _persist_attachments(attachments, self._attachment_service)

        # prompt 隐式 attach：接收消息的 ws 自动跟踪该 session
        self._attach(session_id, ws)

        metadata = InboundMetadata.from_client(frame.get("metadata"))
        metadata = metadata.model_copy(update={"request_id": request_id})

        try:
            payload = {**data, "mode": mode}
            ack = await self._admit(session_id, payload, metadata, kind="user_message")
            if not getattr(ack, "accepted", False):
                error = getattr(ack, "error", None) or {}
                await self._send_rpc(ws, request_id, session_id, ok=False, error={
                    "code": str(error.get("code") or "admission_rejected"),
                    "message": str(error.get("message") or "消息接纳被拒绝"),
                    "session_id": session_id,
                    "retryable": bool(error.get("retryable")),
                })
                return
            await self._send_queue_rpc(ws, request_id, session_id)
        except Exception:
            logger.exception(
                "[ws-channel] durable admission 失败 session=%s request=%s",
                session_id, request_id,
            )
            await self._send_rpc(ws, request_id, session_id, ok=False, error={
                "code": "admission_failed",
                "message": "消息接纳失败，请使用同一 request_id 重试",
                "session_id": session_id,
                "retryable": True,
            })

    async def _on_cancel(self, ws, frame, data, session_id):
        """session.cancel：控制面 request/reply → rpc(accepted)。"""
        if not session_id:
            logger.warning("[ws-channel] cancel 缺少 session_id，忽略")
            return
        self._attach(session_id, ws)
        request_id = frame.get("request_id") or ""
        if not isinstance(request_id, str) or not request_id:
            await self._send_rpc(ws, "", session_id, ok=False, error={
                "code": "missing_request_id",
                "message": "缺少 request_id",
                "session_id": session_id,
                "retryable": False,
            })
            return
        expected_request_id = data.get("expected_request_id") or ""
        try:
            ack = await self.bus.request_inbound(
                BusMessage(
                    type="turn_cancel",
                    from_channel=self.channel_id,
                    from_session=session_id,
                    to_channel=self.channel_id,
                    to_session=session_id,
                    data={
                        "session_id": session_id,
                        "expected_request_id": expected_request_id,
                    },
                )
            )
            await self._send_rpc(
                ws, request_id, session_id,
                ok=True,
                value={"accepted": bool(getattr(ack, "created", False)),
                       "session_id": session_id},
            )
        except (WebSocketDisconnect, RuntimeError):
            logger.debug("[ws-channel] cancel ack skipped after disconnect session=%s", session_id)
        except Exception:
            logger.exception("[ws-channel] cancel control 执行失败 session=%s", session_id)
            await self._send_rpc(ws, request_id, session_id, ok=False, error={
                "code": "control_failed",
                "message": "取消指令执行失败，请重试",
                "session_id": session_id,
                "retryable": True,
            })

    async def _on_queue_update(self, ws: WebSocket, frame: dict, data: dict) -> None:
        """session.updateQueue：edit/remove/steer → rpc(ok+queue)。"""
        session_id = str(data.get("session_id") or "")
        request_id = str(frame.get("request_id") or "")
        item_id = str(data.get("item_id") or "")
        action = data.get("action") or {}
        inbox = self._current_inbox()
        if not session_id or not item_id or not isinstance(action, dict) or inbox is None:
            await self._send_rpc(ws, request_id, session_id, ok=False, error={
                "code": "inbox-unavailable",
                "message": "队列能力不可用",
                "session_id": session_id,
                "retryable": False,
            })
            return
        kind = action.get("kind")
        try:
            snapshot = await inbox.snapshot(session_id)
            steering_ids = {
                item.request_id
                for item in snapshot.next_step
                if getattr(item, "source", "user") == "user"
            }
            if kind in {"edit", "remove"} and item_id in steering_ids:
                await self._send_rpc(ws, request_id, session_id, ok=False, error={
                    "code": "steering-locked",
                    "message": "steering 消息已锁定，不能编辑或移除",
                    "session_id": session_id,
                    "retryable": False,
                })
                return
            if kind == "edit":
                accepted = await inbox.edit(
                    session_id, item_id,
                    _prompt_text(action.get("content")),
                    action.get("attachments"),
                )
            elif kind == "remove":
                accepted = await inbox.remove(session_id, item_id)
            elif kind == "steer":
                if not any(item.request_id == item_id for item in snapshot.next_turn):
                    await self._send_rpc(ws, request_id, session_id, ok=False, error={
                        "code": "steer-not-available",
                        "message": "只有 queued 消息可以提升为 steering",
                        "session_id": session_id,
                        "retryable": False,
                    })
                    return
                accepted = await inbox.promote(session_id, item_id)
            else:
                await self._send_rpc(ws, request_id, session_id, ok=False, error={
                    "code": "invalid_queue_action",
                    "message": "未知队列操作",
                    "session_id": session_id,
                    "retryable": False,
                })
                return
        except Exception:
            logger.exception("[ws-channel] queue update failed session=%s item=%s", session_id, item_id)
            await self._send_rpc(ws, request_id, session_id, ok=False, error={
                "code": "queue_update_failed",
                "message": "队列操作失败",
                "session_id": session_id,
                "retryable": True,
            })
            return
        if not accepted:
            await self._send_rpc(ws, request_id, session_id, ok=False, error={
                "code": "item-not-pending",
                "message": "消息已不在队列中",
                "session_id": session_id,
                "retryable": False,
            })
            return
        await self._send_queue_rpc(ws, request_id, session_id)

    async def _admit(
        self,
        session_id: str,
        data: dict[str, Any] | InboundData,
        metadata: InboundMetadata,
        *,
        kind: str,
    ):
        """把规范化 prompt 信封交给 MessageBus/InBox 边界。"""
        message = BusMessage(
            type=kind,
            from_channel=self.channel_id,
            from_session=session_id,
            to_channel=self.channel_id,
            to_session=session_id,
            data=InboundData.coerce(data).model_dump(),
            metadata=metadata,
        )
        return await self.bus.request_inbound(message)

    # ============================================================
    # attach 基线 / rpc
    # ============================================================

    async def _send_baseline(self, session_id: str, ws: WebSocket) -> None:
        """attach 基线三连（输出锁内）：subscribed → queue → status。"""
        last_seq = -1
        status = "idle"
        if self._sessions_service is not None:
            try:
                last_seq = await self._sessions_service.last_seq(session_id)
            except Exception:  # noqa: BLE001 边界：基线尽力而为
                last_seq = -1
        if self._status_provider is not None:
            try:
                value = self._status_provider(session_id)
                status = await value if asyncio.iscoroutine(value) else str(value)
            except Exception:  # noqa: BLE001 边界
                status = "idle"
        subscribed = SessionSubscribedFrame(
            session_id=session_id,
            payload={"last_seq": last_seq, "status": status},
        )
        inbox = self._current_inbox()
        queue_frame = None
        if inbox is not None:
            try:
                queue_snapshot = await inbox.wire_snapshot(session_id)
                queue_frame = SessionQueueFrame(
                    session_id=session_id, payload=queue_snapshot
                )
            except Exception:  # noqa: BLE001 边界
                queue_frame = None
        try:
            await ws.send_text(
                json.dumps(subscribed.model_dump(mode="json"), ensure_ascii=False, default=str)
            )
            if queue_frame is not None:
                await ws.send_text(
                    json.dumps(queue_frame.model_dump(mode="json"), ensure_ascii=False, default=str)
                )
        except Exception as e:  # noqa: BLE001 边界
            logger.debug(f"[ws-channel] 基线发送失败: {e}")

    async def _send_rpc(
        self, ws, request_id: str, session_id: str, *, ok: bool,
        value: dict | None = None, error: dict | None = None,
    ) -> None:
        """rpc 帧直回（不经 Bus 广播）。"""
        frame = RpcFrame(
            session_id=session_id or "*",
            payload={
                "request_id": request_id or "",
                "ok": ok,
                **({"value": value} if value is not None else {}),
                **({"error": error} if error is not None else {}),
            },
        )
        try:
            await ws.send_text(
                json.dumps(frame.model_dump(mode="json"), ensure_ascii=False, default=str)
            )
        except Exception as e:  # noqa: BLE001 边界
            logger.debug(f"[ws-channel] rpc 回写失败: {e}")

    async def _send_queue_rpc(self, ws: WebSocket, request_id: str, session_id: str) -> None:
        """把操作结算和最新 Inbox wire snapshot 合并成一个 rpc(ok) 响应。"""
        inbox = self._current_inbox()
        if inbox is None:
            await self._send_rpc(ws, request_id, session_id, ok=False, error={
                "code": "inbox-unavailable",
                "message": "队列能力不可用",
                "session_id": session_id,
                "retryable": True,
            })
            return
        try:
            payload = await inbox.wire_snapshot(session_id)
            await self._send_rpc(
                ws, request_id, session_id, ok=True, value=payload
            )
        except Exception:
            logger.exception(
                "[ws-channel] queue response failed session=%s request=%s",
                session_id, request_id,
            )
            await self._send_rpc(ws, request_id, session_id, ok=False, error={
                "code": "queue_snapshot_failed",
                "message": "队列快照读取失败，请使用同一 request_id 重试",
                "session_id": session_id,
                "retryable": True,
            })
