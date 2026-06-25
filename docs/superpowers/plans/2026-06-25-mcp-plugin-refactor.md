# MCP Plugin 化 + register_router API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 MCP 模块封装为内置 Plugin，新增 `FtrePluginApi.register_router()` 让插件可以注册 HTTP 路由。

**Architecture:** FtrePluginApi 新增 `register_router(APIRouter)` 方法，PluginManager 收集所有插件注册的 routers。WebSocketChannel 在挂载路由时遍历 `plugin_manager.routers` 并 `include_router`。MCP 模块的 5 个 CRUD 路由从 `routes.py` 迁移到 `mcp_plugin.py`，McpManager 实例由插件持有。

**Tech Stack:** Python 3.12, FastAPI APIRouter, ftre Plugin 体系

## Global Constraints

- 所有路由保持 `/api` prefix（WebSocketChannel 已有 `prefix="/api"`）
- MCP 插件的路由 prefix 为 `/mcp`，最终路径为 `/api/mcp/...`（与现有完全一致）
- `routes.py` 中 MCP 相关代码全部删除
- `main.py` 中 MCP 初始化代码迁移到插件 `setup()`
- `McpManager.build_system_hint()` 改用 `append_system_prompt()`
- `AgentLoop` 不再需要 `mcp_manager` 参数（system prompt 由插件注入）
- 所有现有测试必须通过

---

## File Structure

| 文件 | 职责 |
| --- | --- |
| `src/ftre/plugin/plugin.py` | 修改：FtrePluginApi 加 `register_router`，PluginManager 加 `routers` property |
| `src/ftre/plugin/builtin/mcp_plugin.py` | 新建：MCP 插件，持有 McpManager，注册路由 + system prompt + 启动连接 |
| `src/ftre/api/routes.py` | 修改：删除 MCP 相关代码（import, 全局变量, set_mcp_manager, 5个路由, 2个辅助函数） |
| `src/ftre/main.py` | 修改：删除 MCP 初始化，WebSocketChannel 挂载插件路由 |
| `src/ftre/channel/ws_channel.py` | 修改：`__init__` 接受 `plugin_manager`，挂载插件 routers |
| `src/ftre/agent/loop.py` | 修改：删除 `mcp_manager` 参数和相关逻辑 |
| `tests/test_plugin_tools.py` | 修改：新增 register_router 测试 |

---

### Task 1: FtrePluginApi.register_router + PluginManager.routers

**Files:**
- Modify: `src/ftre/plugin/plugin.py`
- Test: `tests/test_plugin_tools.py`

**Interfaces:**
- Produces: `FtrePluginApi.register_router(router: APIRouter) -> None`
- Produces: `PluginManager.routers -> list[APIRouter]` (property)
- Produces: `PluginManager._routers: list[APIRouter]` (shared list passed to FtrePluginApi)

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_plugin_tools.py` 末尾：

```python
def test_plugin_api_register_router():
    from fastapi import APIRouter

    class RouterPlugin(Plugin):
        name = "router_plugin"

        def setup(self) -> None:
            router = APIRouter()

            @router.get("/ping")
            def ping():
                return {"pong": True}

            self.api.register_router(router)

    registry = ToolRegistry()
    manager = PluginManager(
        bus=None,
        channel_manager=None,
        session_manager=None,
        hook_manager=HookManager(),
        tool_registry=registry,
    )

    manager._load(RouterPlugin(), {})

    assert len(manager.routers) == 1
    routes = [r.path for r in manager.routers[0].routes]
    assert "/ping" in routes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_plugin_tools.py::test_plugin_api_register_router -v`
Expected: FAIL with `AttributeError: 'FtrePluginApi' object has no attribute 'register_router'`

- [ ] **Step 3: Implement register_router in FtrePluginApi and PluginManager**

在 `src/ftre/plugin/plugin.py` 顶部添加 import：

```python
from fastapi import APIRouter
```

在 `FtrePluginApi.__init__` 中，`self._appended_system_prompts` 行后面加：

```python
        self._routers: list[APIRouter] = routers if routers is not None else []
```

`__init__` 签名加 `routers` 参数：

```python
    def __init__(
        self,
        bus: EventBus,
        channel_manager: ChannelManager,
        session_manager: "SessionManager",
        hook_manager: HookManager,
        config: dict,
        tool_registry: ToolRegistry,
        event_loop: Callable | None = None,
        command_manager: object | None = None,
        appended_system_prompts: list[str] | None = None,
        routers: list[APIRouter] | None = None,
    ):
```

在 `append_system_prompt` 方法后面，`@property appended_system_prompts` 后面加：

```python
    def register_router(self, router: APIRouter) -> None:
        """注册 FastAPI APIRouter，路由会在 WebSocketChannel 启动时挂载到 /api prefix 下。"""
        self._routers.append(router)
```

在 `PluginManager.__init__` 中，`self._appended_system_prompts` 行后面加：

```python
        self._routers: list[APIRouter] = []
```

在 `PluginManager._load` 中，`FtrePluginApi(...)` 调用加 `routers=self._routers`：

```python
        plugin.api = FtrePluginApi(
            bus=self._bus,
            channel_manager=self._channel_manager,
            session_manager=self._session_manager,
            hook_manager=self._hook_manager,
            config=config,
            tool_registry=self._tool_registry,
            event_loop=self._event_loop,
            command_manager=self._command_manager,
            appended_system_prompts=self._appended_system_prompts,
            routers=self._routers,
        )
```

在 `PluginManager.appended_system_prompts` property 后面加：

```python
    @property
    def routers(self) -> list[APIRouter]:
        """获取所有插件注册的 APIRouter。"""
        return self._routers.copy()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_plugin_tools.py::test_plugin_api_register_router -v`
Expected: PASS

- [ ] **Step 5: Run all existing tests**

Run: `python -m pytest tests/test_plugin_tools.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add src/ftre/plugin/plugin.py tests/test_plugin_tools.py
git commit -m "feat(plugin): add FtrePluginApi.register_router() and PluginManager.routers"
```

---

### Task 2: WebSocketChannel 挂载插件路由

**Files:**
- Modify: `src/ftre/channel/ws_channel.py`
- Modify: `src/ftre/main.py`

**Interfaces:**
- Consumes: `PluginManager.routers -> list[APIRouter]` (from Task 1)
- Produces: WebSocketChannel accepts `plugin_manager` param, mounts plugin routers under `/api`

- [ ] **Step 1: Modify WebSocketChannel.__init__ to accept plugin_manager**

在 `src/ftre/channel/ws_channel.py` 中，修改 `WebSocketChannel.__init__` 签名：

```python
    def __init__(self, bus: EventBus, host: str = "0.0.0.0", port: int = 19470, plugin_manager=None):
        super().__init__(channel_id="ws", name="WebSocket Channel", bus=bus)
        self.host = host
        self.port = port
        self.app = FastAPI(title="ftre-gateway")
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self._connections: dict[str, set[WebSocket]] = {}
        self._ws_sessions: dict[WebSocket, set[str]] = {}
        self._server = None
        self._server_task: asyncio.Task | None = None

        # 注册路由
        self.app.websocket("/")(self._ws_endpoint)

        # 挂载 HTTP API 路由
        from ftre.api.routes import router as api_router
        self.app.include_router(api_router, prefix="/api")

        # 挂载插件注册的路由
        if plugin_manager:
            for router in plugin_manager.routers:
                self.app.include_router(router, prefix="/api")
```

- [ ] **Step 2: Modify main.py to pass plugin_manager to WebSocketChannel**

在 `src/ftre/main.py` 中，找到 `WebSocketChannel(bus)` 改为：

```python
    ws_channel = WebSocketChannel(bus, plugin_manager=plugin_manager)
```

- [ ] **Step 3: Verify import works**

Run: `python -c "from ftre.channel.ws_channel import WebSocketChannel; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/ftre/channel/ws_channel.py src/ftre/main.py
git commit -m "feat(ws): WebSocketChannel mounts plugin routers under /api prefix"
```

---

### Task 3: 创建 MCP Plugin

**Files:**
- Create: `src/ftre/plugin/builtin/mcp_plugin.py`
- Modify: `src/ftre/main.py` (remove MCP init code)
- Modify: `src/ftre/agent/loop.py` (remove mcp_manager)
- Modify: `src/ftre/api/routes.py` (remove MCP routes)
- Modify: `src/ftre/mcp/manager.py` (add async_start helper if needed)

**Interfaces:**
- Consumes: `FtrePluginApi.tool_registry`, `FtrePluginApi.append_system_prompt()`, `FtrePluginApi.register_router()`, `FtrePluginApi.event_loop`
- Produces: `McpPlugin` class with `name = "mcp"`, holds `McpManager` instance

- [ ] **Step 1: Create mcp_plugin.py**

创建 `src/ftre/plugin/builtin/mcp_plugin.py`：

```python
"""
MCP Plugin — 将 MCP 模块封装为内置插件

职责：
- 创建 McpManager 实例并管理连接生命周期
- 注册 loadSkill 等工具（通过 tool_registry）
- 注入 MCP 系统提示词（通过 append_system_prompt）
- 注册 MCP CRUD HTTP 路由（通过 register_router）
- 配置热重载（config watcher）
"""
import asyncio
import json
import logging
import os
import tempfile

from fastapi import APIRouter, HTTPException, Request

from ftre.plugin import Plugin
from ftre.mcp.manager import McpManager
from ftre.mcp.adapter import build_mcp_tools
from ftre.config import CONFIG_PATH

logger = logging.getLogger(__name__)


class McpPlugin(Plugin):
    name = "mcp"
    version = "1.0.0"

    def setup(self) -> None:
        cfg = self.api.config or {}
        self._mcp_config = cfg.get("mcp", cfg)
        self._manager = McpManager(tool_registry=self.api.tool_registry)

        # 注入 system prompt
        self.api.append_system_prompt(self._build_system_hint())

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

    async def _start_connections(self) -> None:
        """启动 MCP 服务器连接 + config watcher"""
        await self._manager.start(self._mcp_config, source="startup")

    def _build_system_hint(self) -> str:
        """生成 MCP 系统提示词"""
        return (
            "\n\n## MCP 工具\n"
            "你可以通过 MCP (Model Context Protocol) 调用外部工具。"
            "MCP 工具名格式为 `mcp__{服务器名}__{工具名}`。\n"
            "调用 MCP 工具时，参数会自动传递给对应的 MCP 服务器处理。"
        )

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
        loop = self.api.event_loop
        if loop and self._manager:
            try:
                loop.run_until_complete(self._manager.stop())
            except Exception as e:
                logger.warning(f"[mcp-plugin] teardown error: {e}")


# ─── 辅助函数（从 routes.py 迁移） ──────────────────────────────

def _read_config_json() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _write_config_json(data: dict) -> None:
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
```

- [ ] **Step 2: Remove MCP code from routes.py**

在 `src/ftre/api/routes.py` 中：

1. 删除 `from ftre.mcp.manager import McpManager` (line 24)
2. 删除 `_mcp_manager: McpManager | None = None` (line 38)
3. 删除 `set_mcp_manager` 函数 (lines 59-62)
4. 删除从 `# MCP 服务器管理` 到文件末尾的所有 MCP 路由代码 (lines 568-758)，包括 `_read_config_json`, `_write_config_json`, `_validate_mcp_server`, `list_mcp_servers`, `create_mcp_server`, `update_mcp_server`, `delete_mcp_server`

- [ ] **Step 3: Remove MCP init from main.py**

在 `src/ftre/main.py` 中：

1. 删除 `from ftre.api.routes import set_agent_loop, set_command_manager, set_mcp_manager` 中的 `set_mcp_manager`
2. 删除以下代码块：
```python
    # ── MCP 服务器 ──
    mcp_manager = McpManager(tool_registry=tool_registry)
    mcp_startup_task = asyncio.create_task(
        _start_mcp_background(mcp_manager, config_data.get("mcp", {}))
    )
    set_mcp_manager(mcp_manager)
```
3. 在 `AgentLoop(...)` 调用中删除 `mcp_manager=mcp_manager,`
4. 在 `finally` 块中删除 MCP 清理代码：
```python
        if not mcp_startup_task.done():
            mcp_startup_task.cancel()
            try:
                await mcp_startup_task
            except asyncio.CancelledError:
                pass
```
和
```python
        await mcp_manager.stop()
```

- [ ] **Step 4: Remove mcp_manager from AgentLoop**

在 `src/ftre/agent/loop.py` 中：

1. 从 `__init__` 签名删除 `mcp_manager=None` 参数
2. 删除 `self.mcp_manager = mcp_manager`
3. 在 `_create_agent` 中删除 MCP hint 注入代码块：
```python
        if self.mcp_manager:
            mcp_hint = self.mcp_manager.build_system_hint()
            if mcp_hint:
                system_prompt = system_prompt + mcp_hint
```

- [ ] **Step 5: Pass config_data to plugin_manager so MCP plugin gets mcp config**

在 `src/ftre/main.py` 中确认 `plugin_manager.load_all(config_data)` 调用已存在，且 `config_data` 包含 `mcp` 段。MCP 插件通过 `self.api.config` 获取配置。

确认 `main.py` 中 `plugin_manager.load_all(config_data)` 在 `WebSocketChannel` 创建之前执行。

- [ ] **Step 6: Verify imports**

Run: `python -c "from ftre.plugin.builtin.mcp_plugin import McpPlugin; print('OK')"`
Expected: `OK`

- [ ] **Step 7: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(mcp): migrate MCP module to builtin plugin with register_router

- Create McpPlugin: holds McpManager, registers routes via register_router
- Remove MCP routes from routes.py (5 endpoints + helpers)
- Remove MCP init from main.py
- Remove mcp_manager param from AgentLoop (system prompt via plugin)
- McpPlugin.setup() starts connections async via event_loop.call_soon_threadsafe"
```
