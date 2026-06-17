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
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)

# 带颜色的日志格式
class ColorFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[94m',     # 亮蓝
        'INFO': '\033[92m',      # 亮绿
        'WARNING': '\033[93m',   # 亮黄
        'ERROR': '\033[91m',     # 亮红
        'CRITICAL': '\033[95m',  # 亮紫
    }
    RESET = '\033[0m'

    def format(self, record):
        color = self.COLORS.get(record.levelname, '')
        record.levelname = f"{color}{record.levelname:<8}{self.RESET}"
        record.name = f"\033[37m{record.name}{self.RESET}"  # 白色模块名
        return super().format(record)

# 配置日志：输出到控制台，级别 INFO，带颜色
handler = logging.StreamHandler()
handler.setFormatter(ColorFormatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s'))
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
from ftre.config import load_config_file
from ftre.tools import ToolRegistry
from ftre.tools.cron import CronScheduler
from ftre.mcp import McpManager
from ftre.mcp.config import parse_mcp_config
from ftre.mcp.adapter import build_mcp_tools

# ── 全局 MCP 重载锁 + 函数 ──────────────────────────────────────
# API 路由和 _watch_mcp_config 都会触发重载，用锁防止并发，
# 确保同时只有一条路径在执行 reload + register。
_mcp_reload_lock = asyncio.Lock()
_mcp_manager_ref: McpManager | None = None
_tool_registry_ref: ToolRegistry | None = None
_wlog = logging.getLogger("ftre.mcp.watch")


async def mcp_reload_and_register(raw_mcp: dict, source: str = "unknown") -> None:
    """MCP 热重载 + 工具注册的统一入口。

    任何路径（API 路由、config watcher）需要触发重载时都调这个函数。
    加全局锁防并发，避免工具重复注册。
    """
    if _mcp_manager_ref is None or _tool_registry_ref is None:
        return
    if _mcp_reload_lock.locked():
        _wlog.debug(f"[mcp-config] 已有重载在进行中（来源: {source}），跳过")
        return
    async with _mcp_reload_lock:
        new_configs = parse_mcp_config(raw_mcp)
        await _mcp_manager_ref.reload(new_configs)

        # 刷新 tool_registry 里的 mcp 工具（先移除旧的，再注册新的）
        _tool_registry_ref._tools = [
            t for t in _tool_registry_ref._tools
            if not getattr(t, "name", "").startswith("mcp__")
        ]
        mcp_tools = await build_mcp_tools(_mcp_manager_ref)
        for tool in mcp_tools:
            try:
                _tool_registry_ref.register(tool)
            except ValueError:
                pass  # 已注册则跳过
        _wlog.info(f"[mcp-config] 热重载完成（来源: {source}），注册 {len(mcp_tools)} 个 MCP 工具")


async def _watch_mcp_config(
    mcp_manager: McpManager,
    tool_registry: ToolRegistry,
) -> None:
    """后台协程：轮询 config.json 的 mtime，检测 mcp 段变化后热重连。

    每 3 秒检查一次文件修改时间，避免引入第三方文件监听依赖。
    通过全局 mcp_reload_and_register() 统一入口执行，自带并发锁。
    """
    from ftre.config import CONFIG_PATH
    last_mtime: float = 0.0
    last_mcp_json: str = ""

    if CONFIG_PATH.exists():
        last_mtime = CONFIG_PATH.stat().st_mtime
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            last_mcp_json = json.dumps(raw.get("mcp", {}), sort_keys=True)
        except Exception:
            pass

    while True:
        await asyncio.sleep(3)
        try:
            if not CONFIG_PATH.exists():
                continue
            mtime = CONFIG_PATH.stat().st_mtime
            if mtime == last_mtime:
                continue
            last_mtime = mtime

            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            mcp_json = json.dumps(raw.get("mcp", {}), sort_keys=True)
            if mcp_json == last_mcp_json:
                continue
            last_mcp_json = mcp_json

            # mcp 段变了，通过统一入口热重载
            _wlog.info("[mcp-config] 检测到 config.json mcp 段变化，开始热重载…")
            await mcp_reload_and_register(raw.get("mcp", {}), source="watcher")

        except Exception as e:
            _wlog.warning(f"[mcp-config] 热重载异常: {e}")


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
    from ftre.api.routes import set_agent_loop, set_command_manager, set_mcp_manager
    set_command_manager(cmd)

    # WebSocket Channel
    ws_channel = WebSocketChannel(bus)
    mgr.register(ws_channel)

    # Subagent Channel — 静默通道，承载 task 工具派发的子任务
    mgr.register(SubagentChannel(bus))

    config_data = load_config_file()
    plugin_manager.load_all(config_data)

    # ── MCP 服务器连接 ──
    # 从 config.json 的 "mcp" 段解析配置，连接服务器，发现并注册工具
    mcp_manager = McpManager()
    mcp_configs = parse_mcp_config(config_data.get("mcp", {}))
    if mcp_configs:
        await mcp_manager.start(mcp_configs)
        mcp_tools = await build_mcp_tools(mcp_manager)
        for tool in mcp_tools:
            tool_registry.register(tool)

    # 设置全局引用，供 mcp_reload_and_register() 使用
    global _mcp_manager_ref, _tool_registry_ref
    _mcp_manager_ref = mcp_manager
    _tool_registry_ref = tool_registry

    # 注入 McpManager 到 API 路由
    set_mcp_manager(mcp_manager)

    # 后台协程：监听 config.json mcp 段变化，热重连 MCP 服务器
    asyncio.create_task(_watch_mcp_config(mcp_manager, tool_registry))

    # 全局 AgentLoop（消费所有 session 的消息）
    agent_loop = AgentLoop(
        bus=bus,
        session_manager=session_manager,
        channel_manager=mgr,
        hook_manager=hook_manager,
        tool_registry=tool_registry,
        command_manager=cmd,
        mcp_manager=mcp_manager,
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
        await mcp_manager.stop()
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
