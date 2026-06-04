"""
WebSocket Channel

启动时创建 FastAPI + WebSocket 端点。

连接模型：
- 这是桌面单用户、本机 ws 场景，连接通常只有一条。
- 服务端不维护 session 订阅表：所有 outbound 帧广播给当前所有 ws 连接，
  前端按帧里的 metadata.session_id 自行路由到对应 store。
- 不存在 attach / detach 帧。
"""
import base64
import binascii
import json
import logging
import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from ftre.bus import BusMessage, EventBus
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

    def __init__(self, bus: EventBus, host: str = "0.0.0.0", port: int = 18790):
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
        # 当前所有活跃的 ws 连接；outbound 帧广播给所有连接，
        # 业务路由（按 session_id）由前端完成。
        self._connections: set[WebSocket] = set()
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
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="info")
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
        """Bus outbound → 广播给所有 ws 连接。

        前端拿到帧后按 metadata.session_id 自行路由到对应 store。
        """
        if not self._connections:
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
        for ws in list(self._connections):
            try:
                await ws.send_text(text)
            except Exception as e:
                logger.debug(f"[ws-channel] send 失败，准备关闭: {e}")
                dead.append(ws)

        # 主动关闭坏连接，receive 循环 finally 会兜底从 _connections 移除
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
        self._connections.add(ws)
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
            self._connections.discard(ws)
            logger.info("[ws-channel] connection closed")

    # ============================================================
    # 上行帧处理
    # ============================================================

    async def _on_message(self, raw: str, ws: WebSocket) -> None:
        """
        收到客户端消息 → 投递到 Bus

        上行帧格式: {id, type, data: {...}, metadata?}

        type:
        - user_input 用户消息
        - cancel     取消生成
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
