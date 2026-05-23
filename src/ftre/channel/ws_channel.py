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
        """Bus outbound → 推送给所有 attach 该 session 的 ws"""
        targets = self._connections.get(msg.to_session)
        if not targets:
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
        text = json.dumps(payload, ensure_ascii=False, default=str)

        # 拷贝一份再迭代，避免发送过程中其它路径改动 set
        dead: list[WebSocket] = []
        for ws in list(targets):
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
        logger.info(f"[ws-channel] attach session={session_id}")

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

        # user_input / cancel 隐式 attach（保持向后兼容）
        self._attach(session_id, ws)

        metadata = frame.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        msg = BusMessage(
            type=frame_type,
            from_channel=self.channel_id,
            to_channel=self.channel_id,
            from_session=session_id,
            to_session=session_id,
            data=data,
            metadata=metadata,
        )
        await self.bus.publish_inbound(msg)
