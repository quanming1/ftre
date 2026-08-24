# PRD-C2-MCP双层配置

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | C2 |
| 名称 | MCP 双层配置（公共+私有 MCP + 连接池 + config watcher + CRUD API） |
| 状态 | 已验收 |
| 创建日期 | 2026-08-12 |
| 定稿日期 | 2026-08-12 |
| 验收日期 | 2026-08-12 |
| 关联文档 | docs/TODO.yaml 阶段 C2；AGENTS.md |

## 1. 背景与目标

- **背景**：MCP（Model Context Protocol）服务器需要公共（全局共享，所有 agent 可用）和私有（per-agent 独享）双层管理。公共 MCP 配置在 `config.json`，私有 MCP 配置在 `agent.config.json`。连接需要复用，避免重复加载。
- **目标**：实现 MCP 双层配置——公共/私有分离，连接池全局共享按 server name 去重，config watcher 热重载，HTTP API 按 scope 区分操作目标。
- **非目标**：不实现 MCP 协议本身、不实现外部插件加载机制（C1）。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：公共 MCP config.json——`config.json` 的 `mcp` 段定义公共 MCP 服务器，注册到全局 `tool_registry`，所有 agent 共享
- [x] FR2：私有 MCP agent.config.json——`agent.config.json` 的 `mcp` 段定义私有 MCP 服务器，注册到 per-agent `agent_tool_registry`
- [x] FR3：连接池全局共享——`McpManager._connections` 按 server name 去重，`ensure_connection` 已连接且配置相同则复用，不二次加载
- [x] FR4：ensure_connection 按需连接——在 `BEFORE_AGENT_RUN` hook 中调用 `ensure_connections`，按 agent 配置按需连接
- [x] FR5：config watcher 热重载——监控 `config.json` 的 `mcp` 段变更，自动重连和重新注册工具
- [x] FR6：HTTP API ?scope=global|private——CRUD API 通过 query 参数区分操作公共或私有 MCP

### 2.2 非功能需求

- 性能：MCP 连接复用，相同配置不二次加载
- 安全：私有 MCP 工具不污染全局 registry
- 兼容性：config watcher 变更后平滑切换，不中断正在执行的 turn

## 3. 技术方案

### 模块设计

| 文件 | 职责 |
|---|---|
| `src/ftre/plugin/mcp_plugin.py` | MCP 插件——CRUD API + config watcher + 工具注册 |
| `src/ftre/mcp/` | MCP 管理器——连接池 + ensure_connection + server 通信 |

### 关键数据结构

```python
class McpManager:
    _connections: dict[str, McpConnection]  # server name → 连接（全局共享去重）

    async def ensure_connection(self, server_name: str, config: dict) -> McpConnection: ...

# 配置合并规则
# llm:        provider + model 可覆盖，api_key/base_url/vision 始终用全局
# tools:      整体替换
# mcp:        深度合并（按 server name 为 key，agent 覆盖全局）
```

### 双层配置表

| 层级 | 配置来源 | 注册位置 | 连接管理 |
|---|---|---|---|
| 公共 MCP | `config.json` 的 `mcp` 段 | 全局 `tool_registry` | 启动时 `start_and_register` + config watcher |
| 私有 MCP | `agent.config.json` 的 `mcp` 段 | per-agent `agent_tool_registry` | `BEFORE_AGENT_RUN` hook 中 `ensure_connections` |

### 3.1 当前实现（2026-08-22）

F1-F11 的目录迁移后，以上旧路径和 `BEFORE_AGENT_RUN` 名称仅保留为历史设计记录；当前
运行链路为：`plugins/builtin/mcp/plugin.py` 创建 `McpService`，公共服务器通过 `ToolService`
注册到 global scope，Turn 开始前由 `AgentLoop.tool_registry_for_agent()` 调用
`McpService.prepare_agent()`。匹配公共配置的服务器复用公共连接；新增或覆盖的私有配置按
`server name + 配置` 共享连接池，并只把工具注册到 `agent:<id>` scope，随后由
`ToolService.build_view()` 交给本轮 ReActAgent。禁用的公共服务器通过 Agent restriction
从该 Agent 的视图中隐藏，不污染其他 Agent 或 global registry。

## 4. 接口与一致性边界

- 公共配置来源是 `config.json`，私有配置来源是对应 agent 目录的 `agent.config.json`；两者不能互相写回。
- HTTP CRUD 使用 `scope=global|private` 明确目标；缺少 scope 或 scope 与 agent_id 不匹配时拒绝。
- 连接池以 server name + 有效配置去重；配置变化时旧连接先平滑关闭，再注册新工具。
- `BEFORE_AGENT_RUN` 按当前 Turn 的 agent profile 建立私有连接；连接失败应让当前 Turn 得到明确错误，不污染其他 agent registry。
- MCP 工具调用产生的输入、输出和错误仍由 B3 的 SessionProjection/trace 机制记录；MCP 不直接写 `state.json`。

## 5. 验收标准

- [x] AC1：公共/私有 MCP 工具注册到正确 registry——公共 MCP 工具在全局 registry，私有 MCP 工具在 per-agent registry
- [x] AC2：连接复用不二次加载——相同 server name + 相同配置的 MCP 连接只建立一次
- [x] AC3：config 变更热重载——修改 `config.json` 的 MCP 配置后，config watcher 触发重连和工具重新注册

## 6. 测试计划

- `tests/test_mcp.py`：公共/私有 registry、连接复用、配置变化和失败清理。
- `tests/test_agent_manager.py`：agent profile 与全局 MCP 配置合并。
- 手动验收：两个 agent 使用同名同配置 MCP server 只建立一个连接；修改公共配置不破坏正在执行的 Turn。

## 7. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-08-13 | 补充 MCP HTTP/config/hook/trace 边界和测试计划 | 使 C2 与 AgentLoop/TurnExecutor 的运行生命周期契约一致 |
| 2026-08-13 | 影响复核：仅补充文档；AC1-AC3 未改变，现有 MCP 测试作为当前证据 | 记录协议边界修订不引入代码行为变化 |
| 2026-08-17 | 修复：pyproject 补声明 mcp>=1.0.0,<2.0（此前未声明，CI 按 pyproject 装依赖导致 builtin 插件集成测试 ModuleNotFoundError）；manager.py 兼容 mcp SDK 2.0 的导入名变更（streamablehttp_client → streamable_http_client，try/except 双名导入） | CI 自 0.2.0 发布起连续失败 |
| 2026-08-22 | 修复真实 Gateway 回归：恢复 Agent 私有 MCP 在 Turn 前的连接、工具注册和 per-agent 视图；公共/私有同配置连接复用，禁用项不再泄漏到 Agent；新增私有 scope、连接复用和 Gateway 注入回归测试 | BUG 报告发现 agent.config.json 的 `mcp` 只完成 profile 合并，未被 MCP Feature 消费 |
