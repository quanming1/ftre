"""
ftre 启动入口

启动 WebSocket 服务，串联 Bus → Channel → AgentLoop
"""
import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket

from ftre.bus import EventBus
from ftre.channel import WebSocketChannel, ChannelManager
from ftre.agent.loop import AgentLoop
from ftre.config import DEFAULT_CONFIG

# ─── 全局实例 ─────────────────────────────────────────────────────────

bus = EventBus()
ws_channel = WebSocketChannel(bus)
channel_manager = ChannelManager(bus)
channel_manager.register(ws_channel)

app = FastAPI(title="ftre")


# ─── WebSocket 端点 ───────────────────────────────────────────────────

@app.websocket("/")
async def websocket_endpoint(ws: WebSocket):
    """每个 WS 连接 = 一个 session"""
    await ws.accept()

    # 创建 session
    import uuid
    session_id = uuid.uuid4().hex[:12]
    bus.create_session(session_id)

    # 启动 AgentLoop
    agent_loop = AgentLoop(session_id=session_id, bus=bus, config=DEFAULT_CONFIG)
    agent_loop.start()

    # 注册连接
    ws_channel._connections[session_id] = ws

    try:
        while True:
            raw = await ws.receive_text()
            await ws_channel._on_message(session_id, raw)
    except Exception:
        pass
    finally:
        # 清理
        ws_channel._connections.pop(session_id, None)
        await agent_loop.stop()
        bus.close_session(session_id)


# ─── 生命周期 ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    await channel_manager.start()


@app.on_event("shutdown")
async def shutdown():
    await channel_manager.stop()


# ─── 启动 ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=18790)
