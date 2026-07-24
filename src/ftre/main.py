"""
ftre CLI 入口

本文件做两件事：
1. 配置日志格式（带 ANSI 颜色的 ColorFormatter）
2. 定义 Typer CLI 命令（gateway 启动/停止/状态/日志）

用法：
    ftre gateway                    # 前台启动网关
    ftre gateway --background       # 后台启动网关
    ftre gateway status             # 查看后台进程状态
    ftre gateway stop               # 停止后台进程
    ftre gateway logs               # 查看日志
"""

import asyncio
import json
import logging
import os
import sys

import typer

# ──────────────────────────────────────────────────────────────────────────────
# Windows 控制台 UTF-8 + ANSI 支持
# ──────────────────────────────────────────────────────────────────────────────
# Windows CMD 默认不解析 ANSI 转义码（如 \033[92m = 绿色），
# 导致 ColorFormatter 输出的颜色码原样显示为乱码。
# 这里调用 SetConsoleMode 开启 VIRTUAL_TERMINAL_PROCESSING (0x0004)，
# 让 CMD 能正确解释 ANSI 颜色码。
# 同时强制 stdout/stderr 用 UTF-8 编码，避免 GBK 无法输出 Unicode 字符。
# ──────────────────────────────────────────────────────────────────────────────
if sys.platform == "win32":
    from contextlib import suppress

    import ctypes

    kernel32 = ctypes.windll.kernel32
    stdout = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE = -11
    mode = ctypes.c_uint32()
    if kernel32.GetConsoleMode(stdout, ctypes.byref(mode)):
        kernel32.SetConsoleMode(stdout, mode.value | 0x0004)  # VIRTUAL_TERMINAL_PROCESSING

    # 强制 stdout/stderr 用 UTF-8，无论当前编码是什么
    # 后台模式（重定向到文件）时 Windows 默认用 GBK，导致中文乱码
    with suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ──────────────────────────────────────────────────────────────────────────────
# 日志格式化器 — 给不同模块的日志上不同颜色
# ──────────────────────────────────────────────────────────────────────────────
# 日志输出格式：
#   2025-07-24 12:34:56 - INFO    - ftre.agent - 这是一条日志
#    ↑ 灰色时间          ↑ 级别色   ↑ 模块色     ↑ 白色消息
#
# 每个模块有固定的颜色（NAMESPACE_COLORS），方便在终端快速区分日志来源。
# ──────────────────────────────────────────────────────────────────────────────
class ColorFormatter(logging.Formatter):
    """带 ANSI 颜色的日志格式化器。

    按模块命名空间分配颜色，让终端日志一眼能看出是哪个模块输出的。
    """

    # ANSI 颜色码常量
    RESET = "\033[0m"       # 重置所有样式
    DIM = "\033[2m"        # 暗淡（灰色）
    SEP = "\033[90m"       # 分隔符颜色（深灰）
    MESSAGE = "\033[97m"   # 消息正文颜色（亮白）

    # 日志级别 → ANSI 颜色
    LEVEL_COLORS = {
        "DEBUG": "\033[94m",    # 蓝
        "INFO": "\033[92m",     # 绿
        "WARNING": "\033[93m",  # 黄
        "ERROR": "\033[91m",    # 红
        "CRITICAL": "\033[95m",  # 亮紫
    }

    # 模块命名空间 → ANSI 颜色
    # 不同模块的日志用不同颜色，方便在终端区分
    NAMESPACE_COLORS = {
        "ftre.agent": "\033[95m",           # 亮紫 — Agent 循环
        "ftre.api": "\033[94m",             # 蓝 — HTTP API
        "ftre.bus": "\033[36m",             # 青 — 消息总线
        "ftre.channel": "\033[96m",          # 亮青 — 通道
        "ftre.command": "\033[35m",         # 品红 — 斜杠指令
        "ftre.config": "\033[92m",          # 绿 — 配置
        "ftre.mcp": "\033[38;5;208m",      # 橙 — MCP 协议
        "ftre.plugin": "\033[38;5;141m",    # 紫罗兰 — 插件
        "ftre.session": "\033[38;5;45m",    # 亮青蓝 — 会话管理
        "ftre.tools": "\033[38;5;214m",     # 橙黄 — 工具
        "ftre_agent_core": "\033[38;5;75m", # 浅蓝 — Agent 核心库
        "__main__": "\033[38;5;203m",       # 暗红 — 主入口
    }
    DEFAULT_NAME = "\033[96m"  # 未匹配的模块用默认亮青
    TRACEBACK = "\033[91m"     # 异常堆栈用红色

    def format(self, record: logging.LogRecord) -> str:
        """格式化一条日志记录。

        最终输出形如：
            2025-07-24 12:34:56 - INFO    - ftre.agent - 消息内容
        各部分带有 ANSI 颜色码，终端渲染后是彩色的。
        """
        message = record.getMessage()
        level_color = self.LEVEL_COLORS.get(record.levelname, "")
        message = f"{self.MESSAGE}{message}{self.RESET}"

        # 如果有异常信息，追加红色堆栈
        if record.exc_info:
            traceback = self.formatException(record.exc_info)
            message = f"{message}\n{self.TRACEBACK}{traceback}{self.RESET}"
        if record.stack_info:
            stack = self.formatStack(record.stack_info)
            message = f"{message}\n{self.TRACEBACK}{stack}{self.RESET}"

        # 拼装最终格式：时间 - 级别 - 模块 - 消息
        timestamp = self.formatTime(record, self.datefmt)
        level = f"{level_color}{record.levelname:<8}{self.RESET}"
        name_color = self._name_color(record.name)
        name = f"{name_color}{record.name}{self.RESET}"
        sep = f"{self.SEP}-{self.RESET}"
        return f"{self.DIM}{timestamp}{self.RESET} {sep} {level} {sep} {name} {sep} {message}"

    def _name_color(self, name: str) -> str:
        """根据 logger 名称返回对应的颜色码。

        支持命名空间匹配：ftre.agent.loop 也会匹配 ftre.agent 的颜色。
        """
        for namespace, color in self.NAMESPACE_COLORS.items():
            if name == namespace or name.startswith(f"{namespace}."):
                return color
        return self.DEFAULT_NAME


# ──────────────────────────────────────────────────────────────────────────────
# 日志全局配置
# ──────────────────────────────────────────────────────────────────────────────
# 配置日志：输出到控制台（stderr），级别 INFO
# TTY（终端）→ 用 ColorFormatter 带颜色
# 非 TTY（重定向到文件/管道）→ 用纯文本格式，不带 ANSI 颜色码
handler = logging.StreamHandler()
if sys.stderr.isatty():
    handler.setFormatter(ColorFormatter())
else:
    handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(levelname)-8s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
logging.root.addHandler(handler)
logging.root.setLevel(logging.INFO)

# 降低第三方库的日志级别，避免刷屏
# uvicorn.access 默认 INFO（每个 HTTP 请求一条日志），提到 WARNING
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
# httpx 默认 INFO（每个 LLM API 调用一条日志），提到 WARNING
logging.getLogger("httpx").setLevel(logging.WARNING)


# ──────────────────────────────────────────────────────────────────────────────
# ftre 组件导入
# ──────────────────────────────────────────────────────────────────────────────
# 这些 import 放在日志配置之后，因为某些模块在被 import 时就会创建 logger，
# 如果日志还没配好，那些模块的日志就不会有颜色。
from ftre_agent_core.tool import ToolRegistry
from ftre_agent_core.hooks import FtreCoreHookManager

from ftre.agent.loop import AgentLoop
from ftre.bus import EventBus
from ftre.channel import ChannelManager, SubagentChannel, WebSocketChannel
from ftre.config import load_config_file, load_gateway_address
from ftre.gateway.runtime import GatewayRuntime
from ftre.plugin import HookManager, PluginManager
from ftre.session import SessionManager
from ftre.tools.cron import CronScheduler


# ──────────────────────────────────────────────────────────────────────────────
# run_gateway — 前台启动核心函数
# ──────────────────────────────────────────────────────────────────────────────
# 这是整个后端服务的核心：把所有组件组装起来并启动。
#
# 组件依赖关系：
#
#   SessionManager (SQLite)
#       │
#   EventBus ←── ChannelManager ←── WebSocketChannel（客户端连这个）
#       │                  ←── SubagentChannel（task 工具派发的子任务走这个）
#       │
#   AgentLoop（消费消息 → 调 LLM → 执行工具 → 发回消息）
#       │
#   PluginManager（加载内置/外部插件，注册 Channel/Hook/Tool/Router）
#       │
#   HookManager + CoreHookManager（插件挂到生命周期挂点）
#   ToolRegistry（插件注册工具，Agent 构建工具集时读取）
#   CommandManager（注册斜杠指令如 /compact）
#   CronScheduler（扫描 ~/.ftre/cron/ 触发定时任务）
#
# ──────────────────────────────────────────────────────────────────────────────
async def run_gateway(*, port: int | None = None, host: str | None = None):
    """前台启动 ftre 网关服务。

    这个函数被两种方式调用：
    1. 直接前台启动：用户执行 `ftre gateway`（默认）或 `ftre gateway --foreground`
    2. 后台子进程启动：GatewayRuntime.start() 用 Popen 调 `python -m ftre gateway --foreground`

    Args:
        port: 端口，None 时从 config.json 的 servers.gateway 读取
        host: 绑定地址，None 时从 config.json 读取
    """
    event_loop = asyncio.get_running_loop()

    # ── Session 管理器（SQLite 存储）─────────────────────────
    # 每个 session 对应一个聊天会话，消息历史存在 SQLite 里
    session_manager = SessionManager()
    await session_manager.init()

    # 注入到 HTTP API 路由（前端通过 HTTP 调用 session 管理 API）
    from ftre.api.routes import set_session_manager

    set_session_manager(session_manager)

    # ── 消息总线 ─────────────────────────────────────────────
    # EventBus 是内部消息队列：Channel 收到消息 → 发布到 Bus → AgentLoop 消费
    bus = EventBus()

    # ── Channel 管理器 ───────────────────────────────────────
    # 管理所有通信通道（WebSocket、Subagent 等）
    mgr = ChannelManager(bus)

    # ── Hook 管理器 ─────────────────────────────────────────
    # 让插件能挂到内部生命周期挂点（before_messages_build、before_agent_run 等）
    hook_manager = HookManager()

    # Core Hook 管理器 — 让插件能注册 ON_STOP 等 core 层 hook
    core_hook_manager = FtreCoreHookManager()

    # ── Tool 注册表 ─────────────────────────────────────────
    # 插件注册工具到这里，Agent 构建 Agent 时从这里读取可用工具集
    tool_registry = ToolRegistry()

    # ── Command 管理器 ──────────────────────────────────────
    # 注册斜杠指令（/compact、/clear 等）
    from ftre.command import CommandManager

    cmd = CommandManager()

    # ── Plugin 管理器 ───────────────────────────────────────
    # 加载 ~/.ftre/plugins/ 下的外部插件 + 内置插件（skill、mcp、context_govern、title_gen）
    # 插件可以注册 Channel / Hook / Tool / HTTP Router / System Prompt
    plugin_manager = PluginManager(
        bus=bus,
        channel_manager=mgr,
        session_manager=session_manager,
        hook_manager=hook_manager,
        core_hook_manager=core_hook_manager,
        tool_registry=tool_registry,
        event_loop=lambda: event_loop,
        command_manager=cmd,
    )

    # 注入到 HTTP API 路由
    from ftre.api.routes import set_agent_loop, set_agent_manager, set_command_manager

    set_command_manager(cmd)

    # 加载配置文件 (~/.ftre/config.json)
    config_data = load_config_file()

    # ── Agent 管理器 ────────────────────────────────────────
    # 加载 ~/.ftre/agents/ 下的 per-agent 配置
    # 每个 agent 有独立的 LLM 配置、工具集、MCP、插件、workspace
    from ftre.agent.agent_manager import AgentManager
    from ftre.config import AGENTS_DIR

    agent_manager = AgentManager(agents_dir=AGENTS_DIR)
    agent_manager.ensure_default()  # 确保至少有一个 default agent
    set_agent_manager(agent_manager)

    # 加载所有插件（注册 Channel / Hook / Tool / Router 等）
    plugin_manager.load_all(config_data)

    # ── WebSocket Channel ──────────────────────────────────
    # 这是客户端连接的入口：Electron 前端通过 WebSocket 和后端通信
    # host/port 优先用 CLI 参数，否则从 config.json 的 servers.gateway 读取
    config_host, config_port = load_gateway_address()
    gateway_host = host or config_host
    gateway_port = port or config_port
    ws_channel = WebSocketChannel(
        bus, host=gateway_host, port=gateway_port, plugin_manager=plugin_manager
    )
    mgr.register(ws_channel)

    # Subagent Channel — 静默通道，承载 task 工具派发的子任务
    # 当 Agent 调用 task 工具时，子任务通过这个通道在后台执行
    mgr.register(SubagentChannel(bus))

    # ── 全局 AgentLoop ──────────────────────────────────────
    # 消费所有 session 的消息：收到消息 → 构建 prompt → 调 LLM → 执行工具 → 发回消息
    agent_loop = AgentLoop(
        bus=bus,
        session_manager=session_manager,
        channel_manager=mgr,
        hook_manager=hook_manager,
        core_hook_manager=core_hook_manager,
        tool_registry=tool_registry,
        command_manager=cmd,
        plugin_manager=plugin_manager,
        agent_manager=agent_manager,
    )
    agent_loop.start()
    set_agent_loop(agent_loop)

    # 注册内置斜杠指令（/compact、/clear、/title 等）
    from ftre.command.builtin import register_builtin_commands

    register_builtin_commands(cmd, agent_loop)

    # 启动所有 Channel（WebSocket 监听端口）+ 分发循环
    await mgr.start()

    # ── Cron 调度器 ────────────────────────────────────────
    # 扫描 ~/.ftre/cron/ 触发定时任务（每 30 秒扫描一次）
    cron_scheduler = CronScheduler(
        bus=bus, session_manager=session_manager, channel_manager=mgr
    )
    cron_scheduler.start()

    # ── 保持进程运行，直到 Ctrl+C ─────────────────────────
    # asyncio.sleep(1) 循环让主协程不退出
    # Ctrl+C 触发 KeyboardInterrupt，进入 finally 清理
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        # 清理：按依赖顺序停止各组件
        await cron_scheduler.stop()
        await agent_loop.stop()
        await mgr.stop()
        await session_manager.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Typer CLI 命令定义
# ═══════════════════════════════════════════════════════════════════════════════
# 命令结构：
#
#   ftre                          → 打印帮助（no_args_is_help=True）
#   ftre gateway                  → 前台启动网关
#   ftre gateway --background      → 后台启动网关
#   ftre gateway -p 8000 -H 0.0.0.0  → 指定端口和地址
#   ftre gateway status            → 查看后台进程状态
#   ftre gateway stop              → 停止后台进程
#   ftre gateway logs              → 查看日志
#
# 实现方式：app 是主 Typer，gateway_app 是子 Typer。
# gateway_app 的 callback（invoke_without_command=True）处理 `ftre gateway` 不带子命令的情况，
# 子命令（status/stop/logs）处理各自的子命令调用。
# ═══════════════════════════════════════════════════════════════════════════════

# 主命令组 — 顶层入口
app = typer.Typer(
    name="ftre",
    no_args_is_help=True,      # 不带参数时自动打印帮助
    help="ftre - AI 编程助手",
)

# gateway 子命令组 — 用 invoke_without_command=True 让 `ftre gateway`（无子命令）也能执行 callback
gateway_app = typer.Typer(
    invoke_without_command=True,  # `ftre gateway` 不带子命令时也执行 callback
    no_args_is_help=False,        # `ftre gateway` 不带子命令时不打印帮助，而是执行 callback
    help="启动和管理 ftre 网关服务。",
)
app.add_typer(gateway_app, name="gateway")  # 注册为 `ftre gateway` 子命令组


@gateway_app.callback(invoke_without_command=True)
def gateway(
    ctx: typer.Context,
    port: int | None = typer.Option(None, "--port", "-p", help="网关端口"),
    host: str | None = typer.Option(None, "--host", "-H", help="绑定地址"),
    background: bool = typer.Option(False, "--background", "-d", help="后台运行"),
    foreground: bool = typer.Option(False, "--foreground", help="前台运行（子进程内部用）"),
):
    """启动 ftre 网关服务。

    两种模式：
    - 前台（默认 / --foreground）：直接在当前终端跑，Ctrl+C 退出
    - 后台（--background）：Popen fork 子进程，当前命令立即返回

    --foreground 是给后台子进程用的标记（GatewayRuntime._build_child_command 会加这个参数），
    用户不需要手动传 --foreground（因为它就是默认行为）。

    当用户执行 `ftre gateway status` / `ftre gateway stop` / `ftre gateway logs` 时，
    这个 callback 也会被调用（因为 invoke_without_command=True），但 ctx.invoked_subcommand
    不为 None，所以直接 return，让子命令去执行。
    """
    # 如果带了子命令（status/stop/logs），直接返回，不执行启动逻辑
    if ctx.invoked_subcommand is not None:
        return

    # 互斥检查
    if background and foreground:
        raise typer.BadParameter("--foreground 和 --background 不能同时使用")

    if background:
        # ── 后台启动 ──
        # 创建 GatewayRuntime，用 Popen fork 一个前台子进程
        runtime = GatewayRuntime()
        # 端口/地址优先用 CLI 参数，否则从 config.json 读取
        config_host, config_port = load_gateway_address()
        ok, msg, status = runtime.start(
            port=port or config_port,
            host=host or config_host or "127.0.0.1",
        )
        if ok:
            print("✓ ftre gateway started in background")
            print(f"  PID: {status.pid}")
            print(f"  Port: {status.port}")
            print(f"  Logs: {status.log_path}")
        else:
            print(f"✗ Gateway not started: {msg}")
            raise typer.Exit(1)
        return

    # ── 前台启动（默认或 --foreground）──
    # 直接 asyncio.run(run_gateway())，阻塞在当前终端
    asyncio.run(run_gateway(port=port, host=host))


@gateway_app.command("status")
def gateway_status():
    """查看后台 gateway 进程状态。

    读 ~/.ftre/run/gateway.json 状态文件 + 检查 PID 是否存活。
    如果进程已死，会自动清除状态文件。
    """
    runtime = GatewayRuntime()
    status = runtime.status()
    print(f"Running: {'yes' if status.running else 'no'}")
    print(f"Reason: {status.reason}")
    if status.pid is not None:
        print(f"PID: {status.pid}")
    if status.port is not None:
        print(f"Port: {status.port}")
    if status.started_at is not None:
        print(f"Started At: {status.started_at}")
    print(f"State: {status.state_path}")
    print(f"Logs: {status.log_path}")


@gateway_app.command("stop")
def gateway_stop(
    timeout: int = typer.Option(20, "--timeout", help="停止超时（秒）"),
):
    """停止后台 gateway 进程。

    先发优雅终止信号（SIGTERM / CTRL_BREAK），等待 timeout 秒。
    超时后强制 kill（SIGKILL / taskkill /F）。
    """
    runtime = GatewayRuntime()
    ok, msg, status = runtime.stop(timeout_s=timeout)
    if ok:
        print("✓ Gateway stopped.")
    else:
        print(f"✗ Gateway not stopped: {msg}")
        if status.pid is not None:
            print(f"  PID: {status.pid}")


@gateway_app.command("restart")
def gateway_restart(
    port: int | None = typer.Option(None, "--port", "-p", help="新端口（不指定则沿用上次）"),
    host: str | None = typer.Option(None, "--host", "-H", help="新地址（不指定则沿用上次）"),
    timeout: int = typer.Option(20, "--timeout", help="停止旧进程的超时（秒）"),
):
    """重启后台 gateway 进程。

    改完代码后用这个命令手动重启，加载新代码。
    保留上次的端口和地址（除非用 --port/--host 覆盖）。
    要求 gateway 是后台模式（--background）启动的。
    """
    runtime = GatewayRuntime()
    ok, msg, status = runtime.restart(port=port, host=host, timeout_s=timeout)
    if ok:
        print("✓ Gateway restarted.")
        print(f"  PID: {status.pid}")
        print(f"  Port: {status.port}")
        print(f"  Logs: {status.log_path}")
    else:
        print(f"✗ Gateway not restarted: {msg}")
        raise typer.Exit(1)


@gateway_app.command("logs")
def gateway_logs(
    tail: int = typer.Option(200, "--tail", help="显示最近 N 行日志"),
    follow: bool = typer.Option(True, "--follow/--no-follow", help="持续跟随新日志"),
):
    """查看后台 gateway 日志。

    --follow（默认）：先打印尾部日志，然后持续跟随新日志（类似 tail -f），Ctrl+C 退出。
    --no-follow：只打印最近 N 行日志然后退出。
    """
    runtime = GatewayRuntime()
    if follow:
        # follow_logs 会阻塞直到 Ctrl+C，返回退出码 130
        raise typer.Exit(runtime.follow_logs(tail=tail))
    # 只打印尾部日志
    for line in runtime.read_log_tail(tail=tail):
        print(line)


if __name__ == "__main__":
    app()
