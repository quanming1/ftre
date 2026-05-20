"""
WebSocket Channel

启动时创建 FastAPI + WebSocket 端点。
每个连接 = 一个 session，自动创建 AgentLoop。
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
        self._connections: dict[str, WebSocket] = {}
        self._server = None

        # 注册路由
        self.app.websocket("/")(self._ws_endpoint)

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
        """Bus outbound → 推送给客户端"""
        ws = self._connections.get(msg.to_session)
        if not ws:
            return

        payload = {
            "id": msg.id,
            "type": msg.type,
            "data": msg.data,
            "metadata": {
                **msg.metadata,
                "channel_id": msg.to_channel,
                "session_id": msg.to_session,
            },
        }
        await ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))

    # ============================================================
    # WebSocket 端点
    # ============================================================

    async def _ws_endpoint(self, ws: WebSocket) -> None:
        """每个连接 = 一个 session"""
        await ws.accept()
        session_id = uuid.uuid4().hex[:12]

        # 注册
        self._connections[session_id] = ws
        self.bus.create_session(session_id)

        # 启动 AgentLoop
        from ftre.agent.loop import AgentLoop
        from ftre.config import DEFAULT_CONFIG
        agent_loop = AgentLoop(session_id=session_id, bus=self.bus, config=DEFAULT_CONFIG)
        agent_loop.start()

        logger.info(f"[ws-channel] session connected: {session_id}")

        try:
            while True:
                raw = await ws.receive_text()
                await self._on_message(session_id, raw)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning(f"[ws-channel] session {session_id} error: {e}")
        finally:
            self._connections.pop(session_id, None)
            await agent_loop.stop()
            self.bus.close_session(session_id)
            logger.info(f"[ws-channel] session disconnected: {session_id}")

    async def _on_message(self, session_id: str, raw: str) -> None:
        """收到客户端消息 → 投递到 Bus"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        await self.receive(session_id, data=data)
