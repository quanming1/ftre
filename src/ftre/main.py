"""
ftre CLI 入口

用法：
  ftre gateway    启动 WebSocket 网关服务
"""
import sys
import asyncio

from ftre.bus import EventBus
from ftre.channel import WebSocketChannel, SubagentChannel, ChannelManager
from ftre.session import SessionManager
from ftre.plugin import PluginManager, HookManager
from ftre.agent.loop import AgentLoop
from ftre.config import load_config_file
from ftre.tools import ToolRegistry
from ftre.tools.cron import CronScheduler


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

    # Hook 管理器 — 让插件能挂到内部生命周期挂点
    hook_manager = HookManager()

    # Tool 注册表 — 插件注册工具，Agent 构建工具集时读取
    tool_registry = ToolRegistry()

    # Plugin 管理器 — 加载内置/外部插件（可注册 Channel / Hook / Tool 等）
    plugin_manager = PluginManager(
        bus=bus,
        channel_manager=mgr,
        session_manager=session_manager,
        hook_manager=hook_manager,
        tool_registry=tool_registry,
    )

    # Command 管理器 — 注册斜杠指令
    from ftre.command import CommandManager
    cmd = CommandManager()

    # 全局 AgentLoop（消费所有 session 的消息）
    agent_loop = AgentLoop(
        bus=bus,
        session_manager=session_manager,
        channel_manager=mgr,
        hook_manager=hook_manager,
        tool_registry=tool_registry,
        command_manager=cmd,
    )
    agent_loop.start()

    # 注入到 API 路由（用于 list_sessions 标注 running 状态）
    from ftre.api.routes import set_agent_loop, set_command_manager
    set_agent_loop(agent_loop)
    set_command_manager(cmd)

    # WebSocket Channel
    ws_channel = WebSocketChannel(bus)
    mgr.register(ws_channel)

    # Subagent Channel — 静默通道，承载 task 工具派发的子任务
    mgr.register(SubagentChannel(bus))

    config_data = load_config_file()
    plugin_manager.load_all(config_data)

    # 启动所有 Channel + 分发循环
    await mgr.start()

    # Cron 调度器 — 扫描 ~/.ftre/cron/ 触发定时任务
    cron_scheduler = CronScheduler(bus=bus, session_manager=session_manager, channel_manager=mgr)
    cron_scheduler.start()

    # 保持进程运行，直到 Ctrl+C
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await cron_scheduler.stop()
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
