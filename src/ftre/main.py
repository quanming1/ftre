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


async def run_gateway():
    """启动网关：Bus + SessionManager + WebSocket Channel + ChannelManager 分发循环"""

    # Session 管理器（SQLite）
    session_manager = SessionManager()
    await session_manager.init()

    # 注入到 API 路由
    from ftre.api.routes import set_session_manager
    set_session_manager(session_manager)

    # 消息总线
    bus = EventBus()

    # WebSocket Channel（启动时自动监听 ws://0.0.0.0:18790）
    ws_channel = WebSocketChannel(bus, session_manager=session_manager)

    # Channel 管理器（负责消费 Bus outbound → 分发到对应 Channel）
    mgr = ChannelManager(bus)
    mgr.register(ws_channel)

    # 启动所有 Channel + 分发循环
    await mgr.start()

    # 保持进程运行，直到 Ctrl+C
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
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
