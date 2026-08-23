"""Feature-owned MCP server state and connection scope registry."""
# MCP Feature Service：维护全局与 Agent scoped server 状态、连接复用和工具视图。
# 核心职责：
#   1. start_and_register / reload_and_register —— 全局连接池启停与热重载；
#   2. prepare_agent —— 每个 Agent turn 前按 profile.mcp_config 准备私有工具视图
#      （全局条目复用全局连接，覆盖/新增条目创建该 Agent 私有连接管理器）；
#   3. register_server / list / connect / disconnect —— 状态登记与诊断。
# 不拥有 Session 或 AgentLoop。

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from typing import Any

from .adapter import build_mcp_tools_for_servers
from .config import parse_mcp_config
from .connection import McpManager


@dataclass(frozen=True)
class McpServerState:
    """MCP Server 在某个 scope 下的只读运行状态。

    该模型只用于诊断和路由展示；连接对象与工具 disposer 仍由 McpService 私有
    持有，避免 API 修改实际连接生命周期。
    """
    name: str
    scope: str
    connected: bool
    owner: str


class McpService:
    """Track global/private MCP servers without coupling to a transport adapter."""
    key = "mcp"

    def __init__(self, connection_manager=None, tool_service=None) -> None:
        # (scope, name) → 状态；scope 取值 "global" 或 "agent:<id>"
        self._servers: dict[tuple[str, str], McpServerState] = {}
        self._connections: dict[tuple[str, str], Any] = {}
        self.connection_manager = connection_manager  # 全局连接池
        self._tool_service = tool_service
        self._global_config: dict[str, Any] = {}  # 全局 mcp 配置快照
        # agent 状态：agent_id → (配置签名, 私有连接引用, 工具 disposer 列表, 工具限制)
        self._agent_states: dict[
            str, tuple[str, list[tuple[str, McpManager]], list[object], object | None]
        ] = {}
        # 私有连接管理器按 (name+config) 签名复用：多个 agent 相同配置共享一个 manager
        self._private_managers: dict[str, McpManager] = {}
        self._private_users: dict[str, set[str]] = {}
        self._agent_lock = asyncio.Lock()  # 串行化 prepare/dispose，防并发重复注册

    async def start_and_register(self, raw_config: dict[str, Any]) -> None:
        """Start the Feature-owned MCP connection pool for the raw config."""
        # 启动时：保存全局配置快照并连接全部全局服务器
        self._global_config = copy.deepcopy(raw_config) if isinstance(raw_config, dict) else {}
        if self.connection_manager is not None:
            await self.connection_manager.start_and_register(raw_config)

    async def reload_and_register(self, raw_config: dict[str, Any], source: str = "feature") -> None:
        """Reload the Feature-owned connection pool under its own lock."""
        # 热重载：更新快照并触发 diff 重连（config watcher / API 调用共用）
        self._global_config = copy.deepcopy(raw_config) if isinstance(raw_config, dict) else {}
        if self.connection_manager is not None:
            await self.connection_manager.reload_and_register(raw_config, source=source)

    async def stop(self) -> None:
        """Stop connections and watchers before the Feature Fiber is disposed."""
        # 停服：先清理全部 agent 私有状态，再停全局连接池
        async with self._agent_lock:
            states = list(self._agent_states)
            for agent_id in states:
                await self._dispose_agent_locked(agent_id)
        if self.connection_manager is not None:
            await self.connection_manager.stop()

    async def prepare_agent(self, agent_id: str, raw_config: dict[str, Any] | None) -> None:
        """Prepare the MCP tool view for one Agent without changing global tools.

        Agent profiles contain the global+private merged MCP config. Matching
        global entries reuse the global connection; overridden/new entries get
        a manager scoped to that Agent. Tools are contributed to the existing
        ``ToolService`` scope, so each ReActAgent receives an isolated view.
        """
        # 按 agent 的合并配置（profile.mcp_config）准备私有工具视图：
        #   - 与全局一致的条目 → 复用全局连接的工具（不重复起进程）；
        #   - 私有/覆盖条目 → 创建该 Agent 独立的 McpManager 并连接；
        #   - 工具注册到 scope="agent:<id>"，并对该 agent 屏蔽其未启用的全局 MCP 工具。
        if self._tool_service is None or not agent_id:
            return
        effective = raw_config if isinstance(raw_config, dict) and raw_config else self._global_config
        # 配置签名：JSON 序列化用于判断配置是否变化（避免重复 prepare）
        signature = json.dumps(effective, sort_keys=True, ensure_ascii=False, default=str)
        async with self._agent_lock:
            current = self._agent_states.get(agent_id)
            if (
                current is not None
                and current[0] == signature
                and self._agent_state_ready(current)
            ):
                # 配置未变且就绪 → 直接复用
                return
            if current is not None:
                # 配置变了或上次连接失败 → 先清理旧的
                await self._dispose_agent_locked(agent_id)

            # 区分全局可复用条目与私有条目
            global_configs = {
                cfg.name: cfg for cfg in parse_mcp_config(self._global_config)
            }
            active_configs = {cfg.name: cfg for cfg in parse_mcp_config(effective)}
            private_raw: dict[str, Any] = {}
            reusable_global: set[str] = set()
            for name in active_configs:
                raw_entry = effective.get(name)
                global_entry = self._global_config.get(name)
                if name in global_configs and raw_entry == global_entry:
                    reusable_global.add(name)
                else:
                    private_raw[name] = raw_entry

            scope = f"agent:{agent_id}"
            owner = f"mcp:{agent_id}"
            disposers: list[object] = []
            # 可复用全局工具：从全局连接池构建工具并注册到 agent scope
            if reusable_global and self.connection_manager is not None:
                for tool in await build_mcp_tools_for_servers(
                    self.connection_manager, reusable_global
                ):
                    disposers.append(
                        self._tool_service.register(
                            tool,
                            owner=owner,
                            scope=scope,
                            source="mcp",
                        )
                    )

            # 私有条目：按 (name,config) 签名复用私有 manager，连接后注册工具
            private_refs: list[tuple[str, McpManager]] = []
            for name, raw_entry in private_raw.items():
                manager_key = json.dumps(
                    {"name": name, "config": raw_entry},
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                )
                private_manager = self._private_managers.get(manager_key)
                if private_manager is None:
                    # 首个使用者创建 manager 并连接（继承全局 attachment 服务）
                    private_manager = McpManager(
                        attachment_service=(
                            self.connection_manager.attachment_service
                            if self.connection_manager is not None
                            else None
                        )
                    )
                    await private_manager.start_and_register({name: raw_entry})
                    self._private_managers[manager_key] = private_manager
                    self._private_users[manager_key] = set()
                # 引用计数：多个 agent 共用同一私有 manager
                self._private_users[manager_key].add(agent_id)
                private_refs.append((manager_key, private_manager))
                for tool in await build_mcp_tools_for_servers(
                    private_manager, {name}
                ):
                    disposers.append(
                        self._tool_service.register(
                            tool,
                            owner=owner,
                            scope=scope,
                            source="mcp",
                        )
                    )

            # 屏蔽该 agent 未启用的全局 MCP 工具（保留内置工具）
            denied = self._global_tools_to_deny(
                active_server_names=set(active_configs),
                overridden_server_names=set(private_raw),
            )
            restriction = (
                self._tool_service.restrict(agent_id, owner=owner, deny=denied)
                if denied
                else None
            )
            self._agent_states[agent_id] = (
                signature,
                private_refs,
                disposers,
                restriction,
            )

    @staticmethod
    def _agent_state_ready(
        state: tuple[str, list[tuple[str, McpManager]], list[object], object | None]
    ) -> bool:
        """Retry a same-config Agent when a previous private connect failed."""
        # 就绪判定：有私有连接的必须全部连上；否则要求至少注册了工具或限制
        _, private_refs, disposers, restriction = state
        if private_refs:
            return all(manager.get_connected_servers() for _, manager in private_refs)
        return bool(disposers or restriction is not None)

    def _global_tools_to_deny(
        self,
        *,
        active_server_names: set[str],
        overridden_server_names: set[str],
    ) -> set[str]:
        """Hide disabled/global MCP tools while retaining built-in tools."""
        # 对某 agent 屏蔽：全局已注册的 mcp__ 工具里，其服务器不在该 agent
        # 启用的服务器集合（且未被私有覆盖）中的那些。
        if self.connection_manager is None:
            return set()
        denied: set[str] = set()
        for name in self.connection_manager.registered_tool_names:
            if not name.startswith("mcp__"):
                continue
            server = name[len("mcp__"):].split("__", 1)[0]
            if server not in active_server_names and server not in overridden_server_names:
                denied.add(name)
        return denied

    async def _dispose_agent_locked(self, agent_id: str) -> None:
        """释放一个 agent 的私有视图：摘工具、退引用、必要时停私有 manager。"""
        state = self._agent_states.pop(agent_id, None)
        if state is None:
            return
        _, private_refs, disposers, restriction = state
        if restriction is not None:
            restriction()
        for disposer in disposers:
            disposer()
        # 私有 manager 引用计数递减；无人使用则销毁连接
        for manager_key, manager in private_refs:
            users = self._private_users.get(manager_key)
            if users is None:
                continue
            users.discard(agent_id)
            if users:
                continue
            self._private_users.pop(manager_key, None)
            self._private_managers.pop(manager_key, None)
            await manager.stop()

    def register_server(self, name: str, config: dict[str, Any], scope: str = "global", owner: str = "mcp"):
        """Reserve a scoped server name and return a disposer for Plugin unload."""
        # 状态登记：同名同 scope 不允许重复注册；返回可逆 disposer
        key = (scope, name)
        if key in self._servers:
            raise ValueError(f"MCP server {name!r} already registered in {scope}")
        state = McpServerState(name, scope, False, owner)
        self._servers[key] = state

        def dispose() -> bool:
            return self._servers.pop(key, None) is not None

        return dispose

    def list(self, scope: str | None = None) -> tuple[McpServerState, ...]:
        """List registered MCP servers, optionally limited to global/private scope."""
        return tuple(state for state in self._servers.values() if scope is None or state.scope == scope)

    def is_connected(self, name: str, scope: str = "global") -> bool:
        """Report connection state without exposing transport objects."""
        state = self._servers.get((scope, name))
        return bool(state and state.connected)

    async def connect(self, name: str, scope: str = "global") -> bool:
        """Mark a declared server connected; transport adapters own the socket."""
        # 状态标记为已连接；真正的 socket 由 McpManager 持有
        state = self._servers.get((scope, name))
        if state is None:
            raise KeyError(name)
        self._servers[(scope, name)] = McpServerState(state.name, state.scope, True, state.owner)
        return True

    async def disconnect(self, name: str, scope: str = "global") -> bool:
        """Clear state and drop the scoped connection cache."""
        state = self._servers.get((scope, name))
        if state is None:
            return False
        self._servers[(scope, name)] = McpServerState(state.name, state.scope, False, state.owner)
        self._connections.pop((scope, name), None)
        return True
