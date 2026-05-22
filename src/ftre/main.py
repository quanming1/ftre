"""
ftre CLI 入口

用法：
  ftre gateway    启动 WebSocket 网关服务
"""
import sys
import asyncio

from ftre.bus import EventBus
from ftre.channel import WebSocketChannel, ChannelManager
from ftre.session import SessionManager
from ftre.plugin import PluginManager
from ftre.agent.loop import AgentLoop
from ftre.config import load_config_file


async def run_gateway():
    """启动网关：Bus + SessionManager + AgentLoop + WebSocket Channel + ChannelManager"""

    # Session 管理器（SQLite）
    session_manager = SessionManager()
    await session_manager.init()

    # 注入到 API 路由
    from ftre.api.routes import set_session_manager
    set_session_manager(session_manager)

    # 消息总线
    bus = EventBus()

    # Channel 管理器
    mgr = ChannelManager(bus)

    # 全局 AgentLoop（消费所有 session 的消息）
    agent_loop = AgentLoop(bus=bus, session_manager=session_manager, channel_manager=mgr)
    agent_loop.start()

    # WebSocket Channel
    ws_channel = WebSocketChannel(bus)
    mgr.register(ws_channel)

    # Plugin 管理器 — 加载外部插件（可注册 Channel 等）
    plugin_manager = PluginManager(bus=bus, channel_manager=mgr, session_manager=session_manager)
    config_data = load_config_file()
    plugin_manager.load_all(config_data)

    # 启动所有 Channel + 分发循环
    await mgr.start()

    # 保持进程运行，直到 Ctrl+C
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await agent_loop.stop()
        await mgr.stop()
        await session_manager.close()


def main():
    if len(sys.argv) < 2:
        print("用法: ftre gateway")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "gateway":
        asyncio.run(run_gateway())
    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
