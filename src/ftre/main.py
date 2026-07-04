"""
ftre CLI 入口

用法：
  ftre gateway    启动 WebSocket 网关服务
"""
import sys
import os
import asyncio
import logging
import json

# Windows CMD 默认不认 ANSI 转义码，激活虚拟终端支持
if sys.platform == "win32":
    import ctypes

    kernel32 = ctypes.windll.kernel32
    stdout = kernel32.GetStdHandle(-11)
    mode = ctypes.c_uint32()
    if kernel32.GetConsoleMode(stdout, ctypes.byref(mode)):
        kernel32.SetConsoleMode(stdout, mode.value | 0x0004)


# 带颜色的日志格式
class ColorFormatter(logging.Formatter):
    RESET = "\033[0m"
    DIM = "\033[2m"
    SEP = "\033[90m"
    MESSAGE = "\033[97m"
    LEVEL_COLORS = {
        "DEBUG": "\033[94m",
        "INFO": "\033[92m",
        "WARNING": "\033[93m",
        "ERROR": "\033[91m",
        "CRITICAL": "\033[95m",
    }
    NAMESPACE_COLORS = {
        "ftre.agent": "\033[95m",
        "ftre.api": "\033[94m",
        "ftre.bus": "\033[36m",
        "ftre.channel": "\033[96m",
        "ftre.command": "\033[35m",
        "ftre.config": "\033[92m",
        "ftre.mcp": "\033[38;5;208m",
        "ftre.plugin": "\033[38;5;141m",
        "ftre.session": "\033[38;5;45m",
        "ftre.tools": "\033[38;5;214m",
        "ftre_agent_core": "\033[38;5;75m",
        "__main__": "\033[38;5;203m",
    }
    DEFAULT_NAME = "\033[96m"
    TRACEBACK = "\033[91m"

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        level_color = self.LEVEL_COLORS.get(record.levelname, "")
        message = f"{self.MESSAGE}{message}{self.RESET}"
        if record.exc_info:
            traceback = self.formatException(record.exc_info)
            message = f"{message}\n{self.TRACEBACK}{traceback}{self.RESET}"
        if record.stack_info:
            stack = self.formatStack(record.stack_info)
            message = f"{message}\n{self.TRACEBACK}{stack}{self.RESET}"

        timestamp = self.formatTime(record, self.datefmt)
        level = f"{level_color}{record.levelname:<8}{self.RESET}"
        name_color = self._name_color(record.name)
        name = f"{name_color}{record.name}{self.RESET}"
        sep = f"{self.SEP}-{self.RESET}"
        return f"{self.DIM}{timestamp}{self.RESET} {sep} {level} {sep} {name} {sep} {message}"

    def _name_color(self, name: str) -> str:
        for namespace, color in self.NAMESPACE_COLORS.items():
            if name == namespace or name.startswith(f"{namespace}."):
                return color
        return self.DEFAULT_NAME


# 配置日志：输出到控制台，级别 INFO，带颜色
handler = logging.StreamHandler()
handler.setFormatter(ColorFormatter())
logging.root.addHandler(handler)
logging.root.setLevel(logging.INFO)

# uvicorn HTTP 请求日志默认 INFO 刷屏，提到 WARNING
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
# httpx 的 HTTP 请求日志也刷屏
logging.getLogger("httpx").setLevel(logging.WARNING)

from ftre.bus import EventBus
from ftre.channel import WebSocketChannel, SubagentChannel, ChannelManager
from ftre.session import SessionManager
from ftre.plugin import PluginManager, HookManager
from ftre.agent.loop import AgentLoop
from ftre.config import load_config_file, load_gateway_address
from ftre_agent_core.tool import ToolRegistry
from ftre.tools.cron import CronScheduler


async def run_gateway():
    event_loop = asyncio.get_running_loop()

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

    # Command 管理器 — 注册斜杠指令
    from ftre.command import CommandManager
    cmd = CommandManager()

    # Plugin 管理器 — 加载内置/外部插件（可注册 Channel / Hook / Tool 等）
    plugin_manager = PluginManager(
        bus=bus,
        channel_manager=mgr,
        session_manager=session_manager,
        hook_manager=hook_manager,
        tool_registry=tool_registry,
        event_loop=lambda: event_loop,
        command_manager=cmd,
    )

    # 注入到 API 路由
    from ftre.api.routes import set_agent_loop, set_command_manager, set_agent_manager
    set_command_manager(cmd)

    # 加载配置文件
    config_data = load_config_file()

    # Agent 管理器 — 加载 ~/.ftre/agents/ 下的 per-agent 配置
    from ftre.config import AGENTS_DIR
    from ftre.agent.agent_manager import AgentManager
    agent_manager = AgentManager(agents_dir=AGENTS_DIR, global_config_data=config_data)
    agent_manager.ensure_default()
    set_agent_manager(agent_manager)

    # 加载插件（注册 Channel / Hook / Tool / Router 等）
    plugin_manager.load_all(config_data)

    # WebSocket Channel — host/port 以 config.json 的 servers.gateway 为准
    gateway_host, gateway_port = load_gateway_address()
    ws_channel = WebSocketChannel(
        bus, host=gateway_host, port=gateway_port, plugin_manager=plugin_manager
    )
    mgr.register(ws_channel)

    # Subagent Channel — 静默通道，承载 task 工具派发的子任务
    mgr.register(SubagentChannel(bus))

    # 全局 AgentLoop（消费所有 session 的消息）
    agent_loop = AgentLoop(
        bus=bus,
        session_manager=session_manager,
        channel_manager=mgr,
        hook_manager=hook_manager,
        tool_registry=tool_registry,
        command_manager=cmd,
        plugin_manager=plugin_manager,
        agent_manager=agent_manager,
    )
    agent_loop.start()
    set_agent_loop(agent_loop)

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
