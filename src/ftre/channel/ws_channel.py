"""
WebSocket Channel

上行：客户端发 JSON → 解析为 BusMessage → 投递 Bus
下行：Bus outbound → 直接 JSON 序列化推送给客户端

不做协议转换，BusMessage 就是协议本身。
"""
import json
import uuid
import logging

from fastapi import WebSocket, WebSocketDisconnect

from ftre.bus import BusMessage, EventBus
from .base import Channel

logger = logging.getLogger(__name__)


class WebSocketChannel(Channel):

    def __init__(self, bus: EventBus):
        super().__init__(channel_id="ws", name="WebSocket Channel", bus=bus)
        self._connections: dict[str, WebSocket] = {}

    async def handle_connection(self, ws: WebSocket, session_id: str = None) -> None:
        """处理一个 WebSocket 连接"""
        await ws.accept()
        session_id = session_id or uuid.uuid4().hex[:12]
        self._connections[session_id] = ws

        try:
            while True:
                raw = await ws.receive_text()
                await self._on_message(session_id, raw)
        except WebSocketDisconnect:
            pass
        finally:
            self._connections.pop(session_id, None)

    async def _on_message(self, session_id: str, raw: str) -> None:
        """收到客户端消息 → 投递到 Bus"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        await self.receive(session_id, data=data)

    async def send(self, msg: BusMessage) -> None:
        """Bus outbound → 推送给客户端"""
        ws = self._connections.get(msg.to_session)
        if not ws:
            return

        payload = {
            "id": msg.id,
            "type": msg.type,
            "data": msg.data,
            "metadata": msg.metadata,
        }
        await ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))
