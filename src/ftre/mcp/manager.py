"""MCP 连接管理器：连接服务器、注册工具并监听配置热重载。"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, TYPE_CHECKING

from mcp import ClientSession, Tool as McpToolDef
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client

from .config import McpServerConfig, parse_mcp_config

if TYPE_CHECKING:
    from ftre.tools import ToolRegistry

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
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_connected(self) -> bool:
        return self.session is not None

    async def connect(self) -> bool:
        """连接 MCP 服务器，返回是否成功。

        启动一个专属 task 来持有连接上下文，确保 connect/disconnect
        的 anyio cancel scope 在同一 task 里 enter/exit。
        """
        async with self._connect_lock:
            if self.is_connected:
                return True

            if self._task and not self._task.done():
                await self._cleanup_task()

            self._stop_event.clear()
            ready = asyncio.get_running_loop().create_future()
            self._task = asyncio.create_task(self._run_connection(ready))

            try:
                result = await asyncio.wait_for(ready, timeout=self.config.timeout / 1000)
            except Exception as e:
                logger.warning(f"[mcp] 连接失败: {self.name} — {e}")
                await self._cleanup_task()
                return False

            if result is True:
                logger.info(f"[mcp] 连接成功: {self.name}")
                return True
            await self._cleanup_task()
            return False

    async def _run_connection(self, ready: asyncio.Future) -> None:
        """在专属 task 里管理 MCP 连接的完整生命周期。"""
        try:
            if self.config.type == "local":
                server_params = StdioServerParameters(
                    command=self.config.command[0],
                    args=self.config.command[1:],
                    env=self.config.environment or None,
                )
                async with stdio_client(server_params) as streams:
                    async with ClientSession(*streams) as session:
                        await self._serve_session(session, ready)
            elif self.config.type == "remote":
                async with streamablehttp_client(
                    self.config.url, headers=self.config.headers or None
                ) as (read_stream, write_stream, _):
                    async with ClientSession(read_stream, write_stream) as session:
                        await self._serve_session(session, ready)
            elif not ready.done():
                ready.set_result(False)
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)
            logger.debug(f"[mcp] 连接 task 退出: {self.name} — {e}")
        finally:
            self.session = None

    async def _serve_session(self, session: ClientSession, ready: asyncio.Future) -> None:
        """初始化会话并等待断连信号。"""
        await session.initialize()
        self.session = session
        if not ready.done():
            ready.set_result(True)
        await self._stop_event.wait()

    async def disconnect(self) -> None:
        """断开连接——通知连接 task 自行退出，不跨 task 操作 cancel scope。"""
        self._stop_event.set()
        await self._cleanup_task()

    async def _cleanup_task(self) -> None:
        """等待连接 task 结束并清理。"""
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                # 超时则强制 cancel
                self._task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self._task
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[mcp] 清理连接 task 异常: {self.name} — {e}")
        self._task = None

    async def list_tools(self) -> list[McpToolDef]:
        """列举服务器提供的工具"""
        session = self.session
        if session is None:
            return []
        try:
            result = await session.list_tools()
            return result.tools
        except Exception as e:
            logger.warning(
                f"[mcp] list_tools 失败: {self.name} — {type(e).__name__}: {e}",
                exc_info=True,
            )
            return []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """调用 MCP 工具"""
        session = self.session
        if session is None:
            raise RuntimeError(f"MCP 服务器 {self.name} 未连接")
        return await session.call_tool(tool_name, arguments)


class McpManager:
    """MCP 连接管理器 — 管理所有 MCP 服务器连接、工具注册和配置热重载。

    使用方式：
        mcp_manager = McpManager(tool_registry=tool_registry)
        await mcp_manager.start_and_register(config_data.get("mcp", {}))
        mcp_manager.start_config_watcher()  # 启动后台监听 config.json 变化

    外部只需调 reload_and_register() 触发热重载（API 路由等场景），
    config watcher 和 reload 共享同一个 asyncio.Lock，避免并发重复注册。
    """

    def __init__(self, tool_registry: ToolRegistry | None = None):
        self._connections: dict[str, McpConnection] = {}
        self._tool_registry = tool_registry
        self._reload_lock = asyncio.Lock()
        self._watcher_task: asyncio.Task | None = None

    async def start_and_register(self, raw_mcp: dict) -> None:
        """解析配置、连接服务器、注册工具 — 启动时调用一次。"""
        async with self._reload_lock:
            await self._apply_config(raw_mcp, source="startup")

    async def stop(self) -> None:
        """断开所有连接、停止 config watcher。"""
        if self._watcher_task and not self._watcher_task.done():
            self._watcher_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watcher_task
            self._watcher_task = None
        await asyncio.gather(
            *(conn.disconnect() for conn in self._connections.values()),
            return_exceptions=True,
        )
        self._connections.clear()
        logger.info("[mcp] 所有连接已断开")

    async def reload_and_register(self, raw_mcp: dict, source: str = "unknown") -> None:
        """MCP 热重载 + 工具注册的统一入口。

        任何路径（API 路由、config watcher）需要触发重载时都调这个方法。
        加锁防并发，避免工具重复注册。
        """
        async with self._reload_lock:
            await self._apply_config(raw_mcp, source=source)

    def start_config_watcher(self) -> None:
        """启动后台协程，每 3 秒轮询 config.json 的 mcp 段变化，自动热重载。"""
        if self._watcher_task and not self._watcher_task.done():
            return
        self._watcher_task = asyncio.create_task(self._watch_config())

    async def _watch_config(self) -> None:
        """后台协程：轮询 config.json mtime，检测 mcp 段变化后热重连。"""
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
                logger.info("[mcp] 检测到 config.json mcp 段变化，开始热重载…")
                await self.reload_and_register(raw.get("mcp", {}), source="watcher")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[mcp] config watcher 异常: {e}")

    async def reload(self, configs: list[McpServerConfig]) -> None:
        """热重载：对比新旧配置，断开新增/变更/删除的服务器，保留未变的连接。"""
        new_names = {cfg.name for cfg in configs}
        old_map = {name: conn.config for name, conn in self._connections.items()}
        to_remove = set(self._connections) - new_names

        for cfg in configs:
            old_cfg = old_map.get(cfg.name)
            if old_cfg and old_cfg != cfg:
                to_remove.add(cfg.name)
                logger.info(f"[mcp] 配置变更，重连: {cfg.name}")

        await asyncio.gather(
            *(self._remove_connection(name) for name in to_remove),
            return_exceptions=True,
        )

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
            logger.info(f"[mcp] 热重载: {success}/{len(to_connect)} 连接成功")

    async def _apply_config(self, raw_mcp: dict, source: str) -> None:
        configs = parse_mcp_config(raw_mcp)
        if not configs:
            logger.info("[mcp] 无 MCP 服务器配置")
        await self.reload(configs)
        await self._register_tools()
        logger.info(
            f"[mcp] 配置应用完成（来源: {source}），"
            f"连接 {len(self.get_connected_servers())} 个服务器"
        )

    async def _register_tools(self) -> None:
        """刷新 tool_registry 里的 MCP 工具（先移除旧的，再注册新的）。"""
        if self._tool_registry is None:
            return
        from .adapter import MCP_TOOL_PREFIX, build_mcp_tools

        # 移除旧的 MCP 工具（通过公共 API，同时清理 _tools 和 _inject_map）
        for name in list(self._tool_registry.names):
            if name.startswith(MCP_TOOL_PREFIX):
                self._tool_registry.unregister(name)
        mcp_tools = await build_mcp_tools(self)
        for tool in mcp_tools:
            try:
                self._tool_registry.register(tool)
            except ValueError:
                pass  # 已注册则跳过
        logger.info(f"[mcp] 注册 {len(mcp_tools)} 个 MCP 工具")

    async def list_all_tools(self) -> list[tuple[str, McpToolDef]]:
        """返回所有已连接服务器的工具列表。"""
        all_tools: list[tuple[str, McpToolDef]] = []
        connected = [
            (name, conn)
            for name, conn in self._connections.items()
            if conn.is_connected
        ]
        results = await asyncio.gather(
            *(conn.list_tools() for _, conn in connected),
            return_exceptions=True,
        )
        for (name, _), tools in zip(connected, results):
            if isinstance(tools, Exception):
                logger.warning(f"[mcp] list_tools 失败: {name} — {tools}")
                continue
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
        """生成 MCP 工具的系统提示词片段，注入到 Agent 系统提示词中。"""
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

    async def _remove_connection(self, name: str) -> None:
        conn = self._connections.pop(name, None)
        if conn:
            logger.info(f"[mcp] 移除服务器: {name}")
            await conn.disconnect()

    # ─── Agent 私有 MCP 支持 ──────────────────────────────────────

    async def ensure_connection(self, config: McpServerConfig) -> bool:
        """确保单个服务器已连接，复用已有连接或创建新连接。

        - 已连接且 config 相同 → 复用，不二次加载
        - 已连接但 config 不同 → 断开重连
        - 未连接 → 新建连接

        连接创建后长期存活于连接池，下次调用直接复用。
        """
        existing = self._connections.get(config.name)
        if existing and existing.is_connected and existing.config == config:
            return True

        if existing:
            await self._remove_connection(config.name)

        conn = McpConnection(config)
        self._connections[config.name] = conn
        return await conn.connect()

    async def ensure_connections(self, configs: list[McpServerConfig]) -> set[str]:
        """批量确保服务器已连接。

        Returns:
            成功连接的 server name 集合。
        """
        results = await asyncio.gather(
            *(self.ensure_connection(cfg) for cfg in configs),
            return_exceptions=True,
        )
        success: set[str] = set()
        for cfg, result in zip(configs, results):
            if result is True:
                success.add(cfg.name)
            elif isinstance(result, Exception):
                logger.warning(f"[mcp] ensure_connection 失败: {cfg.name} — {result}")
        logger.info(f"[mcp] ensure_connections: {len(success)}/{len(configs)} 连接成功")
        return success

    async def list_tools_for_servers(
        self, server_names: set[str]
    ) -> list[tuple[str, McpToolDef]]:
        """只列出指定服务器的工具（仅查已连接的）。"""
        targets = [
            (name, conn)
            for name, conn in self._connections.items()
            if name in server_names and conn.is_connected
        ]
        if not targets:
            return []

        results = await asyncio.gather(
            *(conn.list_tools() for _, conn in targets),
            return_exceptions=True,
        )
        all_tools: list[tuple[str, McpToolDef]] = []
        for (name, _), tools in zip(targets, results):
            if isinstance(tools, Exception):
                logger.warning(f"[mcp] list_tools 失败: {name} — {tools}")
                continue
            for tool in tools:
                all_tools.append((name, tool))
        return all_tools

    def build_system_hint_for_servers(self, server_names: list[str]) -> str:
        """为指定服务器列表生成系统提示词片段。"""
        if not server_names:
            return ""
        lines = [
            "",
            "## MCP 工具",
            "你可以通过 MCP (Model Context Protocol) 调用外部工具。MCP 工具名格式为 `mcp__{服务器名}__{工具名}`。",
            f"当前已连接的 MCP 服务器：{', '.join(server_names)}",
            "调用 MCP 工具时，参数会自动传递给对应的 MCP 服务器处理。",
        ]
        return "\n".join(lines)
