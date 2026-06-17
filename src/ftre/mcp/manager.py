"""
MCP 连接管理器

职责：
1. 根据 McpServerConfig 连接 MCP 服务器（stdio / streamable HTTP）
2. 发现工具（listTools）
3. 提供 callTool 能力给 McpToolAdapter
4. 生命周期管理（启动/停止/重连）

设计原则：
- 全异步，所有 IO 操作走 asyncio
- McpManager 实例在 main.py 创建，注入到 AgentLoop
- Agent 运行前从 McpManager 拿到工具列表，注册到 ToolRegistry
- MCP 连接失败不影响主流程，只 log 警告
- disconnect 时直接 terminate 子进程，绕过 anyio cancel scope 跨 task 限制
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp import ClientSession, Tool as McpToolDef
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client

from .config import McpServerConfig

logger = logging.getLogger(__name__)


class McpConnection:
    """单个 MCP 服务器的连接实例。

    关键设计：connect() 和 disconnect() 必须在同一个 asyncio task 里
    成对调用，因为 anyio 的 cancel scope 要求 enter/exit 在同一 task。
    为此，connect() 会创建一个专属 task 来管理连接生命周期，
    disconnect() 通过 _stop_event 通知该 task 自行退出。
    """

    def __init__(self, config: McpServerConfig):
        self.config = config
        self.session: ClientSession | None = None
        self._connected = False
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_connected(self) -> bool:
        return self._connected and self.session is not None

    async def connect(self) -> bool:
        """连接 MCP 服务器，返回是否成功。

        启动一个专属 task 来持有连接上下文，确保 connect/disconnect
        的 anyio cancel scope 在同一 task 里 enter/exit。
        """
        if self._connected:
            return True

        self._stop_event.clear()

        # 用 Future 从子 task 拿到连接结果
        ready = asyncio.get_running_loop().create_future()

        async def _run():
            """在专属 task 里管理 MCP 连接的完整生命周期。"""
            try:
                if self.config.type == "local":
                    server_params = StdioServerParameters(
                        command=self.config.command[0],
                        args=self.config.command[1:],
                        env=self.config.environment or None,
                    )
                    async with stdio_client(server_params) as (read_stream, write_stream):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            self.session = session
                            self._connected = True
                            if not ready.done():
                                ready.set_result(True)
                            # 等待断连信号
                            await self._stop_event.wait()
                elif self.config.type == "remote":
                    async with streamablehttp_client(
                        self.config.url, headers=self.config.headers or None
                    ) as (read_stream, write_stream, _):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            self.session = session
                            self._connected = True
                            if not ready.done():
                                ready.set_result(True)
                            # 等待断连信号
                            await self._stop_event.wait()
                else:
                    if not ready.done():
                        ready.set_result(False)
            except Exception as e:
                if not ready.done():
                    ready.set_exception(e)
                logger.debug(f"[mcp] 连接 task 退出: {self.name} — {e}")

        self._task = asyncio.create_task(_run())

        try:
            result = await asyncio.wait_for(ready, timeout=self.config.timeout / 1000)
            if result is True:
                logger.info(f"[mcp] 连接成功: {self.name}")
                return True
            return False
        except Exception as e:
            logger.warning(f"[mcp] 连接失败: {self.name} — {e}")
            await self._cleanup_task()
            return False

    async def disconnect(self) -> None:
        """断开连接——通知连接 task 自行退出，不跨 task 操作 cancel scope。"""
        self._stop_event.set()
        await self._cleanup_task()
        self.session = None
        self._connected = False

    async def _cleanup_task(self) -> None:
        """等待连接 task 结束并清理。"""
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, Exception):
                # 超时则强制 cancel
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = None

    async def list_tools(self) -> list[McpToolDef]:
        """列举服务器提供的工具"""
        if not self.is_connected:
            return []
        try:
            result = await self.session.list_tools()
            return result.tools
        except Exception as e:
            logger.warning(f"[mcp] list_tools 失败: {self.name} — {e}")
            return []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """调用 MCP 工具"""
        if not self.is_connected:
            raise RuntimeError(f"MCP 服务器 {self.name} 未连接")
        return await self.session.call_tool(tool_name, arguments)


class McpManager:
    """MCP 连接管理器 — 管理所有 MCP 服务器连接"""

    def __init__(self):
        self._connections: dict[str, McpConnection] = {}

    async def start(self, configs: list[McpServerConfig]) -> None:
        """根据配置列表启动所有 MCP 连接"""
        if not configs:
            logger.info("[mcp] 无 MCP 服务器配置")
            return

        for cfg in configs:
            conn = McpConnection(cfg)
            self._connections[cfg.name] = conn

        # 并发连接所有服务器
        results = await asyncio.gather(
            *(conn.connect() for conn in self._connections.values()),
            return_exceptions=True,
        )

        success = sum(1 for r in results if r is True)
        logger.info(f"[mcp] 连接完成: {success}/{len(results)} 成功")

    async def reload(self, configs: list[McpServerConfig]) -> None:
        """热重载：对比新旧配置，断开新增/变更/删除的服务器，保留未变的连接。

        相比 stop() + start()，避免了所有服务器短暂中断。
        """
        new_names = {cfg.name for cfg in configs}
        old_names = set(self._connections.keys())

        # ── 1. 断开已删除的 ──
        removed = old_names - new_names
        for name in removed:
            logger.info(f"[mcp] 移除服务器: {name}")
            await self._connections.pop(name).disconnect()

        # ── 2. 断开配置变更的（命令/url/headers 等变了需要重连） ──
        old_map = {cfg.name: cfg for cfg in self._current_configs()}
        for cfg in configs:
            old_cfg = old_map.get(cfg.name)
            if old_cfg and not self._config_equals(old_cfg, cfg):
                logger.info(f"[mcp] 配置变更，重连: {cfg.name}")
                old_conn = self._connections.pop(cfg.name, None)
                if old_conn:
                    await old_conn.disconnect()

        # ── 3. 连接新增/变更的 ──
        to_connect = [
            cfg for cfg in configs
            if cfg.name not in self._connections or not self._connections[cfg.name].is_connected
        ]
        if to_connect:
            for cfg in to_connect:
                conn = McpConnection(cfg)
                self._connections[cfg.name] = conn

            results = await asyncio.gather(
                *(self._connections[cfg.name].connect() for cfg in to_connect),
                return_exceptions=True,
            )
            success = sum(1 for r in results if r is True)
            logger.info(f"[mcp] 热重载完成: {success}/{len(to_connect)} 连接成功")

        # ── 4. disabled 的标记断开但保留条目 ──
        for cfg in configs:
            if cfg.disabled and cfg.name in self._connections:
                await self._connections[cfg.name].disconnect()

    async def stop(self) -> None:
        """断开所有 MCP 连接"""
        for conn in self._connections.values():
            await conn.disconnect()
        self._connections.clear()
        logger.info("[mcp] 所有连接已断开")

    # ─── 内部辅助 ──────────────────────────────────────

    def _current_configs(self) -> list[McpServerConfig]:
        """收集当前所有连接的配置"""
        return [conn.config for conn in self._connections.values()]

    @staticmethod
    def _config_equals(a: McpServerConfig, b: McpServerConfig) -> bool:
        """判断两份配置是否等价（变更需重连）"""
        return (
            a.name == b.name
            and a.type == b.type
            and a.command == b.command
            and a.environment == b.environment
            and a.url == b.url
            and a.headers == b.headers
            and a.disabled == b.disabled
            and a.timeout == b.timeout
        )

    async def list_all_tools(self) -> list[tuple[str, McpToolDef]]:
        """返回所有已连接服务器的工具列表。

        Returns:
            [(server_name, mcp_tool_def), ...]
        """
        all_tools: list[tuple[str, McpToolDef]] = []
        for name, conn in self._connections.items():
            if not conn.is_connected:
                continue
            tools = await conn.list_tools()
            for tool in tools:
                all_tools.append((name, tool))
        return all_tools

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        """调用指定服务器的工具"""
        conn = self._connections.get(server_name)
        if not conn:
            raise ValueError(f"MCP 服务器不存在: {server_name}")
        return await conn.call_tool(tool_name, arguments)

    def get_connected_servers(self) -> list[str]:
        """返回所有已连接的服务器名"""
        return [name for name, conn in self._connections.items() if conn.is_connected]

    def get_status(self) -> dict[str, str]:
        """返回所有服务器的连接状态"""
        return {
            name: "connected" if conn.is_connected else "disconnected"
            for name, conn in self._connections.items()
        }

    def build_system_hint(self) -> str:
        """生成 MCP 工具的系统提示词片段，注入到 Agent 系统提示词中。

        让 LLM 知道有哪些 MCP 工具可用，以及工具命名规则。
        """
        servers = self.get_connected_servers()
        if not servers:
            return ""

        lines = [
            "",
            "## MCP 工具",
            "你可以通过 MCP (Model Context Protocol) 调用外部工具。MCP 工具名格式为 `mcp__{服务器名}__{工具名}`。",
            f"当前已连接的 MCP 服务器：{', '.join(servers)}",
            "调用 MCP 工具时，参数会自动传递给对应的 MCP 服务器处理。",
        ]
        return "\n".join(lines)
