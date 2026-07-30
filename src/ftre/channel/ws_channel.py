"""
WebSocket Channel

启动时创建 FastAPI + WebSocket 端点。
多个客户端连接共享同一个全局 AgentLoop。

连接 / session 模型：
- 一个客户端 = 一条物理 WebSocket。
- 一条 WebSocket 可以 attach 到多个 session（前端同时关注多个会话）。
- session_id → set[WebSocket]：同一个 session 也允许被多个客户端 attach（多端同步）。
- 客户端必须显式发送 attach 帧（或在 user_message/cancel 时隐式 attach 当前 session），
  后端才会把这条 ws 加入 session 的推送目标。
"""
import base64
import binascii
import json
import logging
import asyncio
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

from ftre.bus import BusMessage, EventBus, GLOBAL_SESSION
from .base import Channel

logger = logging.getLogger(__name__)


# ============================================================
# 附件校验（user_message.data.attachments）
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


def _validate_attachments(attachments) -> tuple[bool, str]:
    """
    校验 user_message.data.attachments。
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


def _persist_attachments(attachments: list | None) -> None:
    """将 attachments 中的 base64 data 落盘，替换为 path。

    在 _validate_attachments 校验通过后调用。原地修改 attachments 列表。
    """
    if not attachments:
        return

    from ftre.utils.image_store import save_image

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
        except Exception:
            logger.warning(f"[ws-channel] 附件落盘失败，跳过: {name}")
            continue

        path = save_image(raw, mime, original_name=name)
        del att["data"]
        att["path"] = path


class WebSocketChannel(Channel):

    def __init__(self, bus: EventBus, host: str = "0.0.0.0", port: int = 48650, plugin_manager=None):
        super().__init__(channel_id="ws", name="WebSocket Channel", bus=bus)
        self.host = host
        self.port = port
        self.app = FastAPI(title="ftre-gateway")
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
        # SessionProjection（由 main.py 注入），attach 时读取 reply/session 快照。
        self._session_projection = None
        self._server = None
        self._server_task: asyncio.Task | None = None

        # 注册路由
        self.app.websocket("/")(self._ws_endpoint)

        # 挂载 HTTP API 路由
        from ftre.api.routes import router as api_router
        self.app.include_router(api_router, prefix="/api")

        # 挂载插件注册的路由
        if plugin_manager:
            for router in plugin_manager.routers:
                self.app.include_router(router, prefix="/api")

    def set_session_projection(self, projection) -> None:
        """注入 SessionProjection（由 main.py 在 AgentLoop 创建后调用）。"""
        self._session_projection = projection

    async def start(self) -> None:
        """启动 WebSocket 服务"""
        import uvicorn
        config = uvicorn.Config(
            self.app, host=self.host, port=self.port,
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
        metadata = dict(msg.metadata or {})

        if msg.to_session == GLOBAL_SESSION:
            targets = list(self._ws_sessions.keys())
        else:
            conns = self._connections.get(msg.to_session)
            if not conns:
                return
            targets = list(conns)

        if not targets:
            return

        payload = {
            "frame_id": msg.id,
            "type": msg.type,
            "data": msg.data,
            "metadata": {
                **metadata,
                "channel_id": msg.to_channel,
                "session_id": msg.to_session,
            },
        }
        text = json.dumps(payload, ensure_ascii=False, default=str)

        # 非全局消息持 session 输出锁：与 attach snapshot 互斥
        lock = self._output_lock(msg.to_session) if msg.to_session != GLOBAL_SESSION else None
        if lock:
            async with lock:
                await self._send_to_targets(targets, text)
        else:
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
            except Exception as e:
                logger.debug(f"[ws-channel] send 失败，准备关闭: {e}")
                dead.append(ws)

        for ws in dead:
            if ws.application_state != WebSocketState.DISCONNECTED:
                try:
                    await ws.close()
                except Exception:
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
        except Exception as e:
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
        收到客户端消息 → 投递到 Bus

        上行帧格式: {id, type, data: {...}, metadata?}

        type:
        - attach     声明这条 ws 关心的 session（data.session_id）
        - detach     取消关心（data.session_id）
        - user_message 用户消息（隐式 attach data.session_id）
        - cancel       取消生成（转为 /cancel user_message，隐式 attach data.session_id）
        """
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(frame, dict):
            return

        frame_type = frame.get("type", "")
        data = frame.get("data") or {}
        if not isinstance(data, dict):
            return
        session_id = data.get("session_id", "")

        if frame_type == "attach":
            # 持 session 输出锁：先发 snapshot，再注册订阅，
            # 保证 snapshot 先于后续实时 Event 到达同一 ws。
            lock = self._output_lock(session_id)
            async with lock:
                await self._send_reply_snapshot(session_id, ws)
                self._attach(session_id, ws)
            return

        if frame_type == "detach":
            self._detach(session_id, ws)
            return

        # ─── cancel 帧：转为 /cancel 的 user_message ───
        # 取消操作统一走系统级 /cancel 指令，不再有 type="cancel" 的 BusMessage
        if frame_type == "cancel":
            if not session_id:
                logger.warning("[ws-channel] cancel 缺少 session_id，忽略")
                return
            self._attach(session_id, ws)
            metadata = frame.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            frame_id = frame.get("frame_id") or ""
            if frame_id:
                metadata = {**metadata, "frame_id": frame_id}
            await self.receive(
                session_id,
                data={"content": "/cancel", "session_id": session_id},
                metadata=metadata,
                kind="user_message",
            )
            return

        # ─── user_confirm_result 帧：工具权限确认结果 ───
        # 前端收到 REQUIRE_USER_CONFIRM 后回传用户决定，驱动 Agent 从挂起恢复。
        if frame_type == "user_confirm_result":
            reply_id = data.get("reply_id", "")
            tool_call_id = data.get("tool_call_id", "")
            approved = data.get("approved")
            if not session_id:
                logger.warning("[ws-channel] user_confirm_result 缺少 session_id，忽略")
                return
            if not reply_id or not tool_call_id or not isinstance(approved, bool):
                await self._reject(
                    ws, frame.get("frame_id", ""), session_id,
                    "user_confirm_result 需要 reply_id、tool_call_id 和布尔 approved",
                )
                return
            self._attach(session_id, ws)
            metadata = frame.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            frame_id = frame.get("frame_id") or ""
            if frame_id:
                metadata = {**metadata, "frame_id": frame_id}
            await self.receive(
                session_id, data, metadata, kind="user_confirm_result"
            )
            return

        if frame_type != "user_message":
            logger.debug(f"[ws-channel] unknown frame type: {frame_type}")
            return

        if not session_id:
            logger.warning(f"[ws-channel] {frame_type} 缺少 session_id，忽略")
            return

        # user_message 附件校验：违规直接拒绝，不进 Bus
        ok, err = _validate_attachments(data.get("attachments"))
        if not ok:
            logger.warning(f"[ws-channel] user_message 附件非法: {err}")
            await self._reject(ws, frame.get("frame_id", ""), session_id, err)
            return

        # 附件落盘：base64 → temp 文件路径，事件链路不再携带 base64
        _persist_attachments(data.get("attachments"))

        # user_message 隐式 attach：接收消息的 ws 自动跟踪该 session
        self._attach(session_id, ws)

        metadata = frame.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        # 把客户端协议帧 id 装进 metadata.frame_id，AgentLoop echo 时
        # 回填给前端，前端用它去重本地乐观占位。
        frame_id = frame.get("frame_id") or ""
        if frame_id:
            metadata = {**metadata, "frame_id": frame_id}

        await self.receive(session_id, data, metadata, kind="user_message")

    async def _send_reply_snapshot(self, session_id: str, ws: WebSocket) -> None:
        """attach 时发送当前进行中 Reply 的完整 Msg 快照。"""
        if self._session_projection is None:
            return
        replies = await self._session_projection.snapshot(session_id)
        session_events = await self._session_projection.session_event_snapshot(session_id)
        if not replies and not session_events:
            return
        payload = {
            "frame_id": f"sync_{session_id}",
            "type": "reply_snapshot",
            "data": {
                "session_id": session_id,
                "replies": replies,
                "events": session_events,
            },
        }
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))
        except Exception as e:
            logger.debug(f"[ws-channel] reply_snapshot 发送失败: {e}")

    async def _reject(self, ws: WebSocket, frame_id: str, session_id: str, reason: str) -> None:
        """向客户端回写一帧拒绝消息（不入 Bus）"""
        payload = {
            "frame_id": frame_id or "",
            "type": "error",
            "data": {
                "code": "invalid_input",
                "message": reason,
                "session_id": session_id,
            },
            "metadata": {
                "channel_id": self.channel_id,
                "session_id": session_id,
            },
        }
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"[ws-channel] reject 回写失败: {e}")
