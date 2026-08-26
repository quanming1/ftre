"""WebSocket Channel：桌面客户端协议到 Bus 的适配层。

启动时创建 FastAPI + WebSocket 端点。
多个客户端连接共享同一个全局 AgentLoop。

连接 / session 模型：
- 一个客户端 = 一条物理 WebSocket。
- 一条 WebSocket 可以 attach 到多个 session（前端同时关注多个会话）。
- session_id → set[WebSocket]：同一个 session 也允许被多个客户端 attach（多端同步）。
- 客户端必须显式发送 attach 帧（或在 session.prompt/session.cancel 时隐式 attach 当前 session），
  后端才会把这条 ws 加入 session 的推送目标。
Channel 只负责连接、帧校验、attach 和 outbound 推送；Session admission、命令解析
和 Agent 执行仍由 MessageBus/AgentLoop 完成。一个连接可以 attach 多个 Session，
因此连接集合是本类私有状态，不能塞进 SessionService。
"""
import asyncio
import base64
import binascii
import json
import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

from ftre.services.attachment import AttachmentService
from ftre.services.messaging.bus import (
    GLOBAL_SESSION,
    BusMessage,
    EventBus,
    InboundData,
    InboundMetadata,
    OutboundMetadata,
)
from ftre.services.messaging.channel.base import Channel

logger = logging.getLogger(__name__)


# ============================================================
# 附件校验（session.prompt.payload.attachments）
# ============================================================

# 允许的图片 MIME
ALLOWED_IMAGE_MIME = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

# 单张附件 base64 解码后的字节数上限
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
    """
    校验 session.prompt.payload.attachments。
    返回 (ok, error_message)。无附件视为合法。
    """
    if attachments is None:
        return True, ""
    if not isinstance(attachments, list):
        return False, "attachments 必须是数组"
    if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
        return False, f"附件数量超过上限 {MAX_ATTACHMENTS_PER_MESSAGE}"

    for i, att in enumerate(attachments):
        if not isinstance(att, dict):
            return False, f"attachments[{i}] 必须是对象"

        att_type = att.get("type")
        if att_type != "image":
            return False, f"attachments[{i}].type 仅支持 'image'，收到 {att_type!r}"

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
    """将 attachments 中的 base64 data 落盘，替换为 path。

    在 _validate_attachments 校验通过后调用。原地修改 attachments 列表。
    """
    if not attachments:
        return

    for att in attachments:
        if not isinstance(att, dict):
            continue
        if att.get("type") != "image":
            continue

        b64 = att.get("data", "")
        mime = att.get("mime_type", "image/png")
        name = att.get("name", "")

        try:
            raw = base64.b64decode(b64)
        except Exception:  # noqa: BLE001 legacy compatibility boundary reviewed in F1
            logger.warning(f"[ws-channel] 附件落盘失败，跳过: {name}")
            continue

        path = attachment_service.save_image(raw, mime, original_name=name)
        del att["data"]
        att["path"] = path


class WebSocketChannel(Channel):
    """维护 WebSocket 连接集合，并把客户端帧转换为 BusMessage。"""

    def __init__(
        self,
        bus: EventBus,
        host: str = "0.0.0.0",
        port: int = 48650,
        app: FastAPI | None = None,
        attachment_service: AttachmentService | None = None,
        http_service=None,
        session_projection=None,
        inbox_provider=None,
        status_provider=None,
    ):
        super().__init__(channel_id="ws", name="WebSocket Channel", bus=bus)
        self.host = host
        self.port = port
        self.app = app
        if self.app is not None:
            self.app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        # session_id → 关注该 session 的 ws 连接集合
        self._connections: dict[str, set[WebSocket]] = {}
        # 反向索引：ws → 它 attach 过的所有 session_id（断开时清理用）
        self._ws_sessions: dict[WebSocket, set[str]] = {}
        # per-session 输出锁：保证 attach snapshot 与实时 Event 的 FIFO 顺序
        self._session_output_locks: dict[str, asyncio.Lock] = {}
        # 这些能力在 Plugin 构造时显式传入；Channel 不提供 setter，也不让
        # Bootstrap 在 FastAPI 物化后回填旧 Service 引用。
        self._session_projection = session_projection
        self._inbox_provider = inbox_provider
        self._status_provider = status_provider
        self._http_service = http_service
        self._server = None
        self._server_task: asyncio.Task | None = None
        # Attachment persistence is a Service dependency; the channel never
        # reaches into the attachment store or constructs a global fallback.
        self._attachment_service = attachment_service

        # Standalone tests may provide an app explicitly. Gateway production
        # startup registers the handler through HttpService instead.
        if self.app is not None:
            self._register_endpoint(self.app)

        # HTTP routes are materialized by HttpService before this Channel is
        # created.  The Channel contributes only the WebSocket protocol path.

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
        # HttpService owns the shared FastAPI Host.  The local app is only a
        # standalone fallback for direct Channel embedding/tests.
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

    async def send(self, msg: BusMessage) -> None:
        """Bus outbound → 推送给 ws 连接。

        - 普通消息：按 to_session 推给所有 attach 该 session 的 ws。
        - 全局广播（to_session == GLOBAL_SESSION）：扇出给所有活跃 ws，
          无视 attach 关系（用于 session 状态等全局控制信号）。
        - 非全局消息持 per-session 输出锁，保证 attach snapshot 与 Event 的 FIFO 顺序。
        """
        metadata = OutboundMetadata.from_inbound(
            msg.metadata,
            channel_id=msg.to_channel,
            session_id=msg.to_session,
        )

        wire_data = (
            msg.data.model_dump(mode="json")
            if hasattr(msg.data, "model_dump")
            else msg.data
        )
        payload = {
            "type": msg.type,
            "payload": wire_data,
            "metadata": metadata.model_dump(exclude_none=True),
        }
        if msg.metadata.request_id:
            payload["request_id"] = msg.metadata.request_id
        text = json.dumps(payload, ensure_ascii=False, default=str)

        if msg.to_session == GLOBAL_SESSION:
            targets = list(self._ws_sessions.keys())
            if targets:
                await self._send_to_targets(targets, text)
            return

        # 先拿锁、再读取订阅者。attach 会在同一把锁内先登记连接再取 snapshot：
        # event 要么排在 snapshot 前并被 snapshot 覆盖，要么排在 snapshot 后实时发送，
        # 不存在“send 先看到无 targets 后直接丢帧”的窗口。
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
            except Exception as e:  # noqa: BLE001 legacy compatibility boundary reviewed in F1
                logger.debug(f"[ws-channel] send 失败，准备关闭: {e}")
                dead.append(ws)

        for ws in dead:
            if ws.application_state != WebSocketState.DISCONNECTED:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001, S110 legacy compatibility boundary reviewed in F1
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
            # Starlette 的 send() 在 OSError 时会把 application_state 设为 DISCONNECTED。
            # 此时 receive_text() 会抛 RuntimeError("WebSocket is not connected...")
            # 而非 WebSocketDisconnect。这是正常的连接断开，不需要 WARNING。
            msg_str = str(e)
            if "not connected" in msg_str:
                logger.debug(f"[ws-channel] connection closed by send failure: {e}")
            else:
                logger.warning(f"[ws-channel] connection error: {e}")
        except Exception as e:  # noqa: BLE001 legacy compatibility boundary reviewed in F1
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
    # 上行帧处理
    # ============================================================

    async def _on_message(self, raw: str, ws: WebSocket) -> None:
        """
        收到客户端消息 → 通过 Bus 交给 Inbox/Command 边界

        上行帧格式: {type, request_id, payload: {...}, metadata?}

        type:
        - attach     声明这条 ws 关心的 session（data.session_id）
        - detach     取消关心（data.session_id）
        - session.prompt 提交 queue/steer 输入（隐式 attach）
        - session.cancel 取消当前 Turn（独立控制消息，不进入 Inbox）
        - session.updateQueue 修改 pending 项
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
            # 持 session 输出锁：先注册再发 snapshot。send() 也在拿到同一锁后
            # 才读取 targets，因此 snapshot 期间到达的 event 不会被漏掉。
            lock = self._output_lock(session_id)
            async with lock:
                self._attach(session_id, ws)
                await self._send_reply_snapshot(session_id, ws)
            return

        if frame_type == "detach":
            self._detach(session_id, ws)
            return

        # ─── cancel 帧：控制面，不伪装成用户消息 ───
        if frame_type == "session.cancel":
            if not session_id:
                logger.warning("[ws-channel] cancel 缺少 session_id，忽略")
                return
            self._attach(session_id, ws)
            request_id = frame.get("request_id") or ""
            if not isinstance(request_id, str) or not request_id:
                await self._reject(ws, "", session_id, "缺少 request_id", code="missing_request_id")
                return
            expected_request_id = data.get("expected_request_id") or ""
            try:
                # 通过 Bus request/reply 进入控制面；它不是 Inbox 输入，
                # 所以不会写 messages，也不会变成待执行的 Agent 输入。
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
                await self._send_control_ack(
                    ws,
                    request_id,
                    session_id,
                    accepted=bool(getattr(ack, "created", False)),
                )
            except (WebSocketDisconnect, RuntimeError):
                logger.debug("[ws-channel] cancel ack skipped after disconnect session=%s", session_id)
            except Exception:
                logger.exception("[ws-channel] cancel control 执行失败 session=%s", session_id)
                await self._reject(
                    ws,
                    request_id,
                    session_id,
                    "取消指令执行失败，请重试",
                    code="control_failed",
                    retryable=True,
                )
            return

        if frame_type == "session.updateQueue":
            await self._on_queue_update(ws, frame, data)
            return

        if frame_type != "session.prompt":
            logger.debug(f"[ws-channel] unknown frame type: {frame_type}")
            return

        if not session_id:
            logger.warning("[ws-channel] session.prompt 缺少 session_id，忽略")
            return

        request_id = frame.get("request_id") or ""
        if not isinstance(request_id, str) or not request_id:
            await self._reject(
                ws,
                "",
                session_id,
                "session.prompt 缺少 request_id，无法保证幂等接纳",
                code="missing_request_id",
                retryable=False,
            )
            return

        mode = data.get("mode") or "queue"
        if mode not in {"queue", "steer"}:
            await self._reject(ws, request_id, session_id, "mode 只能是 queue 或 steer", code="invalid_mode")
            return

        # 附件校验：违规直接拒绝，不进 Bus
        ok, err = _validate_attachments(data.get("attachments"))
        if not ok:
            logger.warning(f"[ws-channel] session.prompt 附件非法: {err}")
            await self._reject(ws, request_id, session_id, err)
            return

        # 附件落盘：base64 → temp 文件路径，事件链路不再携带 base64
        attachments = data.get("attachments")
        if attachments and self._attachment_service is None:
            await self._reject(
                ws,
                request_id,
                session_id,
                "附件服务未就绪，请稍后重试",
                code="attachment_service_unavailable",
                retryable=True,
            )
            return
        if self._attachment_service is not None:
            _persist_attachments(attachments, self._attachment_service)

        # prompt 隐式 attach：接收消息的 ws 自动跟踪该 session
        self._attach(session_id, ws)

        metadata = InboundMetadata.from_client(frame.get("metadata"))
        # request_id 是唯一的传输相关性标识；内部 QueueItem 仅由 Inbox 保存。
        metadata = metadata.model_copy(
            update={
                "request_id": request_id,
            }
        )

        try:
            data = {**data, "mode": mode}
            ack = await self._admit(session_id, data, metadata, kind="user_message")
            if not getattr(ack, "accepted", False):
                error = getattr(ack, "error", None) or {}
                await self._reject(
                    ws, request_id, session_id,
                    str(error.get("message") or "消息接纳被拒绝"),
                    code=str(error.get("code") or "admission_rejected"),
                    retryable=bool(error.get("retryable")),
                )
                return
            await self._send_queue_response(ws, request_id, session_id)
        except Exception:
            logger.exception(
                "[ws-channel] durable admission 失败 session=%s request=%s",
                session_id,
                request_id,
            )
            await self._reject(
                ws,
                request_id,
                session_id,
                "消息接纳失败，请使用同一 request_id 重试",
                code="admission_failed",
                retryable=True,
            )

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

    async def _send_reply_snapshot(self, session_id: str, ws: WebSocket) -> None:
        """attach 时原子发送事件、队列和状态基线。"""
        if (
            self._session_projection is None
            and self._inbox_provider is None
            and self._status_provider is None
        ):
            return
        replies = (
            await self._session_projection.snapshot(session_id)
            if self._session_projection is not None
            else []
        )
        session_events = (
            await self._session_projection.session_event_snapshot(session_id)
            if self._session_projection is not None
            else []
        )
        inbox = self._current_inbox()
        queue = await inbox.wire_snapshot(session_id) if inbox is not None else None
        if not replies and not session_events and queue is None and self._status_provider is None:
            return
        payload = {
            "type": "reply_snapshot",
            "payload": {
                "session_id": session_id,
                "replies": replies,
                "events": session_events,
            },
        }
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))
            if queue is not None:
                await ws.send_text(json.dumps({
                    "type": "session/queue",
                    "payload": queue,
                }, ensure_ascii=False, default=str))
            status = "idle"
            if self._status_provider is not None:
                value = self._status_provider(session_id)
                status = await value if asyncio.iscoroutine(value) else str(value)
            await ws.send_text(json.dumps({
                "type": "session/status",
                "payload": {
                    "session_id": session_id,
                    "status": status,
                },
            }, ensure_ascii=False, default=str))
        except Exception as e:  # noqa: BLE001 legacy compatibility boundary reviewed in F1
            logger.debug(f"[ws-channel] reply_snapshot 发送失败: {e}")

    async def _on_queue_update(self, ws: WebSocket, frame: dict, data: dict) -> None:
        """将 edit/remove/steer 转给 Inbox Package，不在 Channel 维护队列。"""
        session_id = str(data.get("session_id") or "")
        request_id = str(frame.get("request_id") or "")
        item_id = str(data.get("item_id") or "")
        action = data.get("action") or {}
        inbox = self._current_inbox()
        if not session_id or not item_id or not isinstance(action, dict) or inbox is None:
            await self._reject(ws, request_id, session_id, "队列能力不可用", code="inbox-unavailable")
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
                await self._reject(
                    ws,
                    request_id,
                    session_id,
                    "steering 消息已锁定，不能编辑或移除",
                    code="steering-locked",
                )
                return
            if kind == "edit":
                accepted = await inbox.edit(
                    session_id,
                    item_id,
                    _prompt_text(action.get("content")),
                    action.get("attachments"),
                )
            elif kind == "remove":
                accepted = await inbox.remove(session_id, item_id)
            elif kind == "steer":
                if not any(item.request_id == item_id for item in snapshot.next_turn):
                    await self._reject(
                        ws,
                        request_id,
                        session_id,
                        "只有 queued 消息可以提升为 steering",
                        code="steer-not-available",
                    )
                    return
                accepted = await inbox.promote(session_id, item_id)
            else:
                await self._reject(ws, request_id, session_id, "未知队列操作", code="invalid_queue_action")
                return
        except Exception:
            logger.exception("[ws-channel] queue update failed session=%s item=%s", session_id, item_id)
            await self._reject(ws, request_id, session_id, "队列操作失败", code="queue_update_failed", retryable=True)
            return
        if not accepted:
            await self._reject(ws, request_id, session_id, "消息已不在队列中", code="item-not-pending")
            return
        await self._send_queue_response(ws, request_id, session_id)

    async def _reject(
        self,
        ws: WebSocket,
        request_id: str,
        session_id: str,
        reason: str,
        *,
        code: str = "invalid_input",
        retryable: bool = False,
    ) -> None:
        """用统一 RPC error envelope 回写，不伪装成 Queue 快照。"""
        payload = {
            "request_id": request_id or "",
            "ok": False,
            "error": {
                "code": code,
                "message": reason,
                "session_id": session_id,
                "retryable": retryable,
            },
        }
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001 legacy compatibility boundary reviewed in F1
            logger.debug(f"[ws-channel] reject 回写失败: {e}")

    async def _send_control_ack(
        self,
        ws: WebSocket,
        request_id: str,
        session_id: str,
        *,
        accepted: bool,
    ) -> None:
        payload = {
            "request_id": request_id,
            "ok": True,
            "value": {"accepted": accepted, "session_id": session_id},
        }
        await ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def _send_queue_response(
        self, ws: WebSocket, request_id: str, session_id: str
    ) -> None:
        """把操作结算和最新 Inbox wire snapshot 合并成一个响应。"""
        inbox = self._current_inbox()
        if inbox is None:
            await self._reject(
                ws,
                request_id,
                session_id,
                "队列能力不可用",
                code="inbox-unavailable",
                retryable=True,
            )
            return
        try:
            payload = await inbox.wire_snapshot(session_id)
            response = {
                "type": "session/queue",
                "request_id": request_id,
                "ok": True,
                "payload": payload,
            }
            # 与 attach/后台 push 共用 Session 输出锁，避免同一连接看到
            # operation response 先于更旧的 queue snapshot。
            async with self._output_lock(session_id):
                await ws.send_text(json.dumps(response, ensure_ascii=False))
        except Exception:
            logger.exception(
                "[ws-channel] queue response failed session=%s request=%s",
                session_id,
                request_id,
            )
            await self._reject(
                ws,
                request_id,
                session_id,
                "队列快照读取失败，请使用同一 request_id 重试",
                code="queue_snapshot_failed",
                retryable=True,
            )
