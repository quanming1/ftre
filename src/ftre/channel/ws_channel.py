"""
WebSocket Channel

启动时创建 FastAPI + WebSocket 端点。
多个客户端连接共享同一个全局 AgentLoop。
"""
import uuid
import json
import logging
import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from ftre.bus import BusMessage, EventBus
from .base import Channel

logger = logging.getLogger(__name__)


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
        # session_id → WebSocket 映射（一个 session 对应一个活跃连接）
        self._connections: dict[str, WebSocket] = {}
        self._server = None

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
        asyncio.create_task(self._server.serve())
        logger.info(f"[ws-channel] listening on ws://{self.host}:{self.port}/")

    async def stop(self) -> None:
        """停止服务"""
        if self._server:
            self._server.should_exit = True
        logger.info("[ws-channel] stopped")

    async def send(self, msg: BusMessage) -> None:
        """Bus outbound → 推送给客户端（按 session_id 找 WebSocket）"""
        ws = self._connections.get(msg.to_session)
        if not ws:
            return

        payload = {
            "id": msg.id,
            "type": msg.type,
            "data": msg.data,
            "metadata": {
                "channel_id": msg.to_channel,
                "session_id": msg.to_session,
            },
        }
        await ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))

    # ============================================================
    # WebSocket 端点
    # ============================================================

    async def _ws_endpoint(self, ws: WebSocket) -> None:
        """WebSocket 连接入口"""
        await ws.accept()
        # 当前连接绑定的 session_id（首次发消息时注册）
        bound_session_id: str | None = None

        logger.info("[ws-channel] connection established")

        try:
            while True:
                raw = await ws.receive_text()
                session_id = self._on_message(raw, ws)
                if session_id and not bound_session_id:
                    bound_session_id = session_id
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning(f"[ws-channel] connection error: {e}")
        finally:
            # 清理连接映射
            if bound_session_id:
                self._connections.pop(bound_session_id, None)
            logger.info(f"[ws-channel] connection closed (session={bound_session_id})")

    def _on_message(self, raw: str, ws: WebSocket) -> str | None:
        """
        收到客户端消息 → 投递到 Bus

        上行帧格式: {id, type, data: {content, session_id, ...}, metadata?}
        返回 session_id（用于连接绑定）
        """
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            return None

        frame_type = frame.get("type", "")
        data = frame.get("data", {})
        metadata = frame.get("metadata", {})
        session_id = data.get("session_id", "")

        if not session_id:
            logger.warning("[ws-channel] 收到无 session_id 的消息，忽略")
            return None

        # 注册 session_id → WebSocket 映射（后续 outbound 按此推送）
        self._connections[session_id] = ws

        if frame_type == "user_input":
            msg = BusMessage(
                type="user_input",
                from_channel=self.channel_id,
                to_channel=self.channel_id,
                from_session=session_id,
                to_session=session_id,
                data=data,
                metadata=metadata,
            )
            asyncio.ensure_future(self.bus.publish_inbound(msg))
        elif frame_type == "cancel":
            cancel_msg = BusMessage(
                type="cancel",
                from_channel=self.channel_id,
                to_channel=self.channel_id,
                from_session=session_id,
                to_session=session_id,
                data=data,
                metadata=metadata,
            )
            asyncio.ensure_future(self.bus.publish_inbound(cancel_msg))
        else:
            logger.debug(f"[ws-channel] unknown frame type: {frame_type}")

        return session_id
