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

from fastapi import APIRouter, HTTPException, Request

from ftre.plugin import BEFORE_AGENT_RUN, Plugin, append_to_first_system
from ftre.mcp.manager import McpManager
from ftre.config import CONFIG_PATH

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

    def _inject_system_prompt(self, ctx):
        prompt = (
            "<mcp_desc>\n"
            "你可以通过 MCP (Model Context Protocol) 调用外部工具。"
            "MCP 工具名格式为 `mcp__{服务器名}__{工具名}`。\n"
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
        """构建 MCP CRUD 路由"""
        router = APIRouter(prefix="/mcp")

        @router.get("")
        async def list_mcp_servers():
            config_data = _read_config_json()
            mcp_raw = config_data.get("mcp", {})
            status_map = self._manager.get_status()
            servers = []
            for name, c in (mcp_raw or {}).items():
                if not isinstance(c, dict):
                    continue
                entry = {**c, "name": name}
                entry["status"] = status_map.get(name, "disconnected")
                servers.append(entry)
            return {"servers": servers}

        @router.post("", status_code=201)
        async def create_mcp_server(request: Request):
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

            config_data = _read_config_json()
            mcp = config_data.setdefault("mcp", {})
            if name in mcp:
                raise HTTPException(status_code=409, detail=f"MCP 服务器已存在: {name}")
            mcp[name] = cleaned
            _write_config_json(config_data)

            if not cleaned.get("disabled"):
                await self._manager.reload_and_register({name: cleaned}, source="api-create")

            return {"name": name, **cleaned, "status": "connected" if not cleaned.get("disabled") else "disabled"}

        @router.patch("/{name}")
        async def update_mcp_server(name: str, request: Request):
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
            _write_config_json(config_data)

            await self._manager.reload_and_register(mcp, source="api-update")
            return {"name": name, **cleaned}

        @router.delete("/{name}", status_code=204)
        async def delete_mcp_server(name: str):
            config_data = _read_config_json()
            mcp = config_data.get("mcp", {})
            if name not in mcp:
                raise HTTPException(status_code=404, detail=f"MCP 服务器不存在: {name}")

            del mcp[name]
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

    return {}, "未知 type"
