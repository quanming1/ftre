"""
WebSocket Channel

启动时创建 FastAPI + WebSocket 端点。
多个客户端连接共享同一个全局 AgentLoop。

连接 / session 模型：
- 一个客户端 = 一条物理 WebSocket。
- 一条 WebSocket 可以 attach 到多个 session（前端同时关注多个会话）。
- session_id → set[WebSocket]：同一个 session 也允许被多个客户端 attach（多端同步）。
- 客户端必须显式发送 attach 帧（或在 user_input/cancel 时隐式 attach 当前 session），
  后端才会把这条 ws 加入 session 的推送目标。
"""
import base64
import binascii
import json
import logging
import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from ftre.bus import BusMessage, EventBus, GLOBAL_SESSION
from .base import Channel

logger = logging.getLogger(__name__)


# ============================================================
# 附件校验（user_input.data.attachments）
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

# 单条消息允许的最大附件数
MAX_ATTACHMENTS_PER_MESSAGE = 8


def _validate_attachments(attachments) -> tuple[bool, str]:
    """
    校验 user_input.data.attachments。
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


class WebSocketChannel(Channel):

    def __init__(self, bus: EventBus, host: str = "0.0.0.0", port: int = 19470):
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
        self._server = None
        self._server_task: asyncio.Task | None = None

        # 注册路由
        self.app.websocket("/")(self._ws_endpoint)

        # 挂载 HTTP API 路由
        from ftre.api.routes import router as api_router
        self.app.include_router(api_router, prefix="/api")

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

    async def send(self, msg: BusMessage) -> None:
        """Bus outbound → 推送给 ws 连接。

        - 普通消息：按 to_session 推给所有 attach 该 session 的 ws。
        - 全局广播（to_session == GLOBAL_SESSION）：扇出给所有活跃 ws，
          无视 attach 关系（用于 session 状态等全局控制信号）。
        """
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
            "id": msg.id,
            "type": msg.type,
            "data": msg.data,
            "metadata": {
                **(msg.metadata or {}),
                "channel_id": msg.to_channel,
                "session_id": msg.to_session,
            },
        }
        text = json.dumps(payload, ensure_ascii=False, default=str)

        # 拷贝一份再迭代，避免发送过程中其它路径改动 set
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(text)
            except Exception as e:
                logger.debug(f"[ws-channel] send 失败，准备关闭: {e}")
                dead.append(ws)

        # 主动关闭坏连接，receive 循环 finally 会兜底清理索引
        for ws in dead:
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
        - user_input 用户消息（隐式 attach data.session_id）
        - cancel     取消生成（隐式 attach data.session_id）
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
            self._attach(session_id, ws)
            return

        if frame_type == "detach":
            self._detach(session_id, ws)
            return

        if frame_type not in ("user_input", "cancel"):
            logger.debug(f"[ws-channel] unknown frame type: {frame_type}")
            return

        if not session_id:
            logger.warning(f"[ws-channel] {frame_type} 缺少 session_id，忽略")
            return

        # user_input 附件校验：违规直接拒绝，不进 Bus
        if frame_type == "user_input":
            ok, err = _validate_attachments(data.get("attachments"))
            if not ok:
                logger.warning(f"[ws-channel] user_input 附件非法: {err}")
                await self._reject(ws, frame.get("id", ""), session_id, err)
                return

        # user_input / cancel 隐式 attach（保持向后兼容）
        self._attach(session_id, ws)

        metadata = frame.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        # 把客户端协议帧 id 装进 metadata.frame_id，AgentLoop echo 时
        # 回填给前端，前端用它去重本地乐观占位。
        frame_id = frame.get("id") or ""
        if frame_id:
            metadata = {**metadata, "frame_id": frame_id}

        await self.receive(session_id, data, metadata, kind=frame_type)

    async def _reject(self, ws: WebSocket, frame_id: str, session_id: str, reason: str) -> None:
        """向客户端回写一帧拒绝消息（不入 Bus）"""
        payload = {
            "id": frame_id or "",
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
