"""
MCP Plugin — 将 MCP 模块封装为内置插件

职责：
- 创建 McpManager 实例并管理连接生命周期
- 注册 MCP 工具（通过 tool_registry）
- 注入 MCP 系统提示词（通过 before_agent_run，向 messages 前插入 system 消息）
- 注册 MCP CRUD HTTP 路由（通过 register_router）
- 配置热重载（config watcher）
"""
import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ftre.plugin import BEFORE_AGENT_RUN, Plugin, append_to_first_system
from ftre.mcp.manager import McpManager
from ftre.mcp.config import McpServerConfig
from ftre.mcp.adapter import build_mcp_tools_for_servers
from ftre.config import CONFIG_PATH, AGENTS_DIR

logger = logging.getLogger(__name__)


class McpPlugin(Plugin):
    name = "mcp"
    version = "1.0.0"

    def setup(self) -> None:
        # MCP 配置在 config.json 顶层 mcp 段，不在 plugins 数组里，直接读文件
        config_data = _read_config_json()
        self._mcp_config = config_data.get("mcp", {})
        self._manager = McpManager(tool_registry=self.api.tool_registry)

        self.api.register_hook(BEFORE_AGENT_RUN, self._inject_system_prompt)

        # 注册 HTTP 路由
        self.api.register_router(self._build_router())

        # 异步启动连接（延迟到事件循环）
        loop = self.api.event_loop
        if loop:
            loop.call_soon_threadsafe(
                asyncio.create_task,
                self._start_connections()
            )
        else:
            logger.warning("[mcp-plugin] 无事件循环，MCP 连接未启动")

    async def _inject_system_prompt(self, ctx):
        """BEFORE_AGENT_RUN hook：为 agent 注入私有 MCP 工具 + 系统提示词。

        流程：
        1. 从 profile.mcp_config 找出私有/覆盖的 server（不在全局 config.json 或配置不同）
        2. ensure_connections → 连入共享连接池（已连接且配置相同则跳过）
        3. build_mcp_tools_for_servers → 构建 Tool 列表
        4. 注册到 ctx.agent_tool_registry（per-agent registry）
        5. 注入系统提示词（列出所有已连接 server）
        """
        profile = ctx.agent_profile

        # 读取全局 MCP 配置，用于区分公共/私有
        global_mcp = _read_config_json().get("mcp", {})

        # 找出私有或覆盖的 server
        private_configs: list[McpServerConfig] = []
        if profile and profile.mcp_config:
            for name, raw in profile.mcp_config.items():
                if not isinstance(raw, dict):
                    continue
                global_raw = global_mcp.get(name)
                if global_raw == raw:
                    # 与全局配置完全一致 → 已通过全局 registry 注册，跳过
                    continue
                # 私有（不在全局）或覆盖（配置不同）
                cfg = McpServerConfig.from_raw(name, raw)
                if cfg:
                    private_configs.append(cfg)

        # 连接 + 注册私有 MCP 工具
        if private_configs and ctx.agent_tool_registry:
            connected = await self._manager.ensure_connections(private_configs)
            if connected:
                tools = await build_mcp_tools_for_servers(self._manager, connected)
                for tool in tools:
                    ctx.agent_tool_registry.register(tool)
                logger.info(
                    f"[mcp-plugin] agent={profile.agent_id if profile else '?'}: "
                    f"注册 {len(tools)} 个私有 MCP 工具 "
                    f"(servers={list(connected)})"
                )

        # 注入系统提示词（列出所有已连接 server，含全局 + 私有）
        all_connected = self._manager.get_connected_servers()
        if all_connected:
            prompt = (
                "<mcp_desc>\n"
                "你可以通过 MCP (Model Context Protocol) 调用外部工具。"
                "MCP 工具名格式为 `mcp__{服务器名}__{工具名}`。\n"
                f"当前已连接的 MCP 服务器：{', '.join(all_connected)}\n"
                "调用 MCP 工具时，参数会自动传递给对应的 MCP 服务器处理。"
                "\n</mcp_desc>"
            )
            append_to_first_system(ctx.messages, prompt)

        return ctx

    async def _start_connections(self) -> None:
        """启动 MCP 服务器连接 + config watcher"""
        try:
            await self._manager.start_and_register(self._mcp_config)
            self._manager.start_config_watcher()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[mcp-plugin] 后台启动失败")

    def _build_router(self) -> APIRouter:
        """构建 MCP CRUD 路由

        通过 ?scope=global|private 区分操作目标：
        - global  → config.json 的 mcp 段（公共，所有 agent 共享）
        - private → agent.config.json 的 mcp 段（私有，仅对该 agent 可见）
        private scope 需额外传 ?agent_id=xxx（默认 "default"）
        """
        router = APIRouter(prefix="/mcp")

        @router.get("")
        async def list_mcp_servers(scope: str = "all", agent_id: str = "default"):
            """列出 MCP 服务器。

            scope=all     → 公共 + 私有合并返回（每条带 scope 字段）
            scope=global  → 仅公共
            scope=private → 仅私有
            """
            status_map = self._manager.get_status()
            servers = []

            if scope in ("all", "global"):
                global_mcp = _read_config_json().get("mcp", {})
                for name, c in (global_mcp or {}).items():
                    if not isinstance(c, dict):
                        continue
                    entry = {**c, "name": name, "scope": "global"}
                    entry["status"] = status_map.get(name, "disconnected")
                    servers.append(entry)

            if scope in ("all", "private"):
                agent_mcp = _read_agent_config_json(agent_id).get("mcp", {})
                for name, c in (agent_mcp or {}).items():
                    if not isinstance(c, dict):
                        continue
                    # all 模式下，如果 agent 覆盖了同名的全局 server，更新它
                    if scope == "all":
                        # 找到并替换已有的 global 条目
                        for i, s in enumerate(servers):
                            if s["name"] == name:
                                entry = {**c, "name": name, "scope": "private"}
                                entry["status"] = status_map.get(name, "disconnected")
                                servers[i] = entry
                                break
                        else:
                            entry = {**c, "name": name, "scope": "private"}
                            entry["status"] = status_map.get(name, "disconnected")
                            servers.append(entry)
                    else:
                        entry = {**c, "name": name, "scope": "private"}
                        entry["status"] = status_map.get(name, "disconnected")
                        servers.append(entry)

            return {"servers": servers}

        @router.post("", status_code=201)
        async def create_mcp_server(request: Request, scope: str = "global", agent_id: str = "default"):
            try:
                payload = await request.json()
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=400, detail=f"非法 JSON: {e}")
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")

            name = payload.get("name", "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="name 不能为空")
            if not all(c.isalnum() or c in "-_" for c in name):
                raise HTTPException(status_code=400, detail="name 只允许字母、数字、连字符、下划线")

            cleaned, err = _validate_mcp_server(payload)
            if err:
                raise HTTPException(status_code=400, detail=err)

            if scope == "private":
                config_data = _read_agent_config_json(agent_id)
                mcp = config_data.setdefault("mcp", {})
                if name in mcp:
                    raise HTTPException(status_code=409, detail=f"MCP 服务器已存在: {name}")
                mcp[name] = cleaned
                _write_agent_config_json(agent_id, config_data)
            else:
                config_data = _read_config_json()
                mcp = config_data.setdefault("mcp", {})
                if name in mcp:
                    raise HTTPException(status_code=409, detail=f"MCP 服务器已存在: {name}")
                mcp[name] = cleaned
                _write_config_json(config_data)
                if not cleaned.get("disabled"):
                    await self._manager.reload_and_register({name: cleaned}, source="api-create")

            return {"name": name, **cleaned, "scope": scope, "status": "connected" if not cleaned.get("disabled") else "disabled"}

        @router.patch("/{name}")
        async def update_mcp_server(name: str, request: Request, scope: str = "global", agent_id: str = "default"):
            if scope == "private":
                config_data = _read_agent_config_json(agent_id)
                mcp = config_data.get("mcp", {})
            else:
                config_data = _read_config_json()
                mcp = config_data.get("mcp", {})

            if name not in mcp:
                raise HTTPException(status_code=404, detail=f"MCP 服务器不存在: {name}")

            try:
                payload = await request.json()
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=400, detail=f"非法 JSON: {e}")
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")

            merged = {**mcp[name], **payload}
            cleaned, err = _validate_mcp_server(merged)
            if err:
                raise HTTPException(status_code=400, detail=err)

            mcp[name] = cleaned

            if scope == "private":
                _write_agent_config_json(agent_id, config_data)
            else:
                _write_config_json(config_data)
                await self._manager.reload_and_register(mcp, source="api-update")

            return {"name": name, **cleaned, "scope": scope}

        @router.delete("/{name}", status_code=204)
        async def delete_mcp_server(name: str, scope: str = "global", agent_id: str = "default"):
            if scope == "private":
                config_data = _read_agent_config_json(agent_id)
                mcp = config_data.get("mcp", {})
            else:
                config_data = _read_config_json()
                mcp = config_data.get("mcp", {})

            if name not in mcp:
                raise HTTPException(status_code=404, detail=f"MCP 服务器不存在: {name}")

            del mcp[name]

            if scope == "private":
                _write_agent_config_json(agent_id, config_data)
            else:
                _write_config_json(config_data)
                await self._manager.reload_and_register(mcp, source="api-delete")

            return None

        return router

    def teardown(self) -> None:
        """清理 MCP 连接"""
        if self._manager:
            try:
                loop = self.api.event_loop
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(self._manager.stop(), loop).result(timeout=5)
            except Exception as e:
                logger.warning(f"[mcp-plugin] teardown error: {e}")


# ─── 辅助函数（从 routes.py 迁移） ──────────────────────────────

def _read_config_json() -> dict:
    """读取 config.json 原始内容"""
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _write_config_json(data: dict) -> None:
    """原子写入 config.json"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".config.", suffix=".tmp", dir=str(CONFIG_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _agent_config_path(agent_id: str) -> Path:
    """返回指定 agent 的 agent.config.json 路径"""
    return AGENTS_DIR / agent_id / "agent.config.json"


def _read_agent_config_json(agent_id: str) -> dict:
    """读取指定 agent 的 agent.config.json"""
    path = _agent_config_path(agent_id)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_agent_config_json(agent_id: str, data: dict) -> None:
    """原子写入指定 agent 的 agent.config.json"""
    path = _agent_config_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".agent.config.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _validate_mcp_server(payload: dict) -> tuple[dict, str | None]:
    """校验单个 MCP 服务器配置，返回 (cleaned, error)"""
    if not isinstance(payload, dict):
        return {}, "配置必须是 JSON 对象"

    server_type = payload.get("type", "")
    if server_type not in ("local", "remote"):
        return {}, "type 必须是 'local' 或 'remote'"

    disabled = bool(payload.get("disabled", False))

    if server_type == "local":
        command = payload.get("command", [])
        if not isinstance(command, list) or not command:
            return {}, "local 类型必须提供非空 command 数组"
        if not all(isinstance(c, str) for c in command):
            return {}, "command 数组元素必须是字符串"
        env = payload.get("environment")
        if env is not None and not isinstance(env, dict):
            return {}, "environment 必须是 dict"
        return {
            "type": "local",
            "command": command,
            "environment": env or {},
            "disabled": disabled,
            "timeout": int(payload.get("timeout", 30_000)),
        }, None

    elif server_type == "remote":
        url = payload.get("url", "")
        if not isinstance(url, str) or not url:
            return {}, "remote 类型必须提供 url"
        headers = payload.get("headers")
        if headers is not None and not isinstance(headers, dict):
            return {}, "headers 必须是 dict"
        return {
            "type": "remote",
            "url": url,
            "headers": headers or {},
            "disabled": disabled,
            "timeout": int(payload.get("timeout", 30_000)),
        }, None
