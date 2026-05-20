"""
ftre CLI 入口

用法：
  ftre gateway    启动 WebSocket 网关服务
"""
import sys
import asyncio

from ftre.bus import EventBus
from ftre.channel import WebSocketChannel, ChannelManager


async def run_gateway():
    """启动网关：Bus + WebSocket Channel + ChannelManager 分发循环"""

    # 消息总线
    bus = EventBus()

    # WebSocket Channel（启动时自动监听 ws://0.0.0.0:18790）
    ws_channel = WebSocketChannel(bus)

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
