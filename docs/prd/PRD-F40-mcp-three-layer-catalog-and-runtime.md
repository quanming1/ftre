# PRD-F40 MCP 三层配置、统一目录与运行时收口

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F40 |
| 名称 | MCP 三层配置、统一目录与运行时收口 |
| 状态 | approved |
| 创建日期 | 2026-09-02 |
| 定稿日期 | 2026-09-02 |
| 验收日期 | — |
| 关联文档 | `docs/TODO.yaml` F40；配对桌面端 C2；历史 C2 MCP 双层配置 |

## 1. 背景与目标

- **背景**：当前 MCP 已分裂为配置、运行连接和 HTTP 诊断三套状态。`McpManager._connections` 持有真实连接，`/api/mcp` 却读取没有生产者的 `McpService._servers`，导致客户端永久空列表；旧客户端仍按 CRUD 协议消费，后端只保留了 GET。Agent 私有配置能在 Turn 前被惰性装配，却不能在 UI 中发现；工作区 `.ftre/mcp.json` 只会被创建，从未被加载。
- **目标**：以 `McpService` 为 MCP 配置解析、目录快照、连接生命周期和 HTTP 契约的唯一 Owner，使全局、Agent 和项目三层配置在 UI 与 ToolService 运行时都遵循同一优先级。
- **非目标**：不实现新的 MCP 传输协议、不修改 Agent ReAct/LLM 协议、不让 ToolService 解析 MCP 文件、不在本阶段做 MCP 市场、OAuth、远程凭据保管或运行中 Turn 的热替换。

## 2. 需求范围

### 2.1 功能需求

- [ ] FR1：支持三层 MCP 来源：全局 `~/.ftre/config.json#mcp`、Agent `~/.ftre/agents/<agent>/agent.config.json#mcp`、项目 `<workspace>/.ftre/mcp.json#mcp`。
- [ ] FR2：统一解析优先级为 `project > agent > global`；同名项只保留优先级最高的有效定义作为 effective 项，所有来源仍可在管理视图中展示并标注覆盖关系。
- [ ] FR3：`McpService.catalog(agent_id, workspace, view)` 返回配置快照，不依赖连接是否已建立；状态至少区分 `configured`、`connecting`、`connected`、`failed`、`disabled`、`invalid`。
- [ ] FR4：连接由 `McpManager` 保持；全局连接注册到 global ToolService scope，Agent 覆盖注册到 `agent:<agent_id>`，项目覆盖注册到 `session:<session_id>`。同名/同配置连接按配置指纹复用，不泄漏运行时前缀到 API/UI。
- [ ] FR5：ToolService 增加 session 作用域投影，固定可见性顺序为 `global → agent → session`；会话级限制不能影响同 Agent 的其他 Session。
- [ ] FR6：恢复 MCP HTTP API：`GET` 查询 effective/source 快照；`POST/PATCH/DELETE` 明确指定 `scope=global|agent|project`，Agent 操作必须带 `agent_id`，项目操作必须带 `workspace`。写入必须委托 ConfigService、AgentProfileService、WorkspaceService，不得在 MCP Plugin 中直接操作其文件。
- [ ] FR7：全局配置变更订阅 ConfigService watcher 后重新加载连接和全局工具；Agent/项目配置变更在下一次对应 ToolView 创建时按新快照装配。运行中的 ToolView 保持不可变。
- [ ] FR8：API 对环境变量和远程 headers 做脱敏；损坏/禁用配置可诊断、可展示，不能导致其他合法 MCP 消失。
- [ ] FR9：删除无生产者的 `_servers`、伪 `connect/disconnect` 状态 API 与 `McpManager` 私有文件轮询 watcher，不保留兼容分支。

### 2.2 非功能需求

- **隔离性**：同一个 Agent 的不同工作区同时运行时，项目 MCP 和禁用规则互不污染。
- **安全性**：HTTP 不返回 `environment`、`headers` 中的秘密值；只暴露字段名及脱敏占位。
- **一致性**：配置写入原子化；无效编辑保留最后一个运行中连接和 ToolView，但 catalog 如实报告错误。
- **性能**：目录查询仅读取三个小配置来源，不连接服务器；连接只在 Plugin 启动或 ToolView 准备时发生。

## 3. 技术方案

```text
ConfigService ─────── global mcp ─┐
AgentProfileService ─ agent mcp ──┼─> McpService.resolve()
WorkspaceService ─── project mcp ─┘        │
                                         McpCatalogSnapshot
                                           │          │
                                     HTTP / UI    McpManager pool
                                                        │
                                ToolService: global → agent → session
                                                        │
                                                   Agent ToolView
```

| 模块 | 职责 |
|---|---|
| `plugins/builtin/mcp/service.py` | 三层 resolve、catalog、CRUD 编排、连接池复用、Agent/Session ToolView 装配 |
| `plugins/builtin/mcp/router.py` | 薄 HTTP 适配层，只调用 McpService 公开方法 |
| `plugins/builtin/mcp/connection.py` | 单连接与配置 diff；不再自己轮询配置文件 |
| `services/agent_profile/` | 增加 Agent MCP 源读取/写入窄 API，不暴露 Manager 路径细节 |
| `services/workspace/` | 增加项目 MCP 源读取/原子写入窄 API，维护 `.ftre/mcp.json` 唯一文件边界 |
| `services/tools/` | 增加 session scope 与 session restriction，不认识 MCP 配置 |

### 3.1 数据模型

```python
McpCatalogItem(
    name: str,
    scope: Literal["global", "agent", "project"],
    status: Literal["configured", "connecting", "connected", "failed", "disabled", "invalid"],
    config: Mapping[str, object],  # 已脱敏
    effective: bool,
    shadowed_by: str | None,
    error: str | None,
    tools_count: int,
)
```

`scope` 表示配置文件 Owner，不表示 ToolService 的内部 scope。API 永远不返回 `agent:<id>`、`session:<id>` 或连接池 key。

### 3.2 配置与运行态边界

- Catalog 总是从三个配置 Source 构建，即使服务器尚未连接也要返回 `configured`。
- 只有实际 ToolView 准备过程会创建 Agent/Session 连接；连接失败只影响该 effective item 的状态。
- 全局 ConfigService 变更通过 `config.watch()` 触发 `reload_global()`；不得继续使用 MCP 自己的 mtime 轮询。
- Agent/项目 CRUD 只修改目标层，不能把继承来的定义拷贝写入下层。

## 4. 接口定义

### 4.1 查询

```http
GET /api/mcp?agent_id=default&workspace=E:/repo&view=effective
GET /api/mcp?agent_id=default&workspace=E:/repo&view=sources
```

`view=effective` 默认返回每个名称最终生效的一项；`view=sources` 返回三层全部项，并通过 `effective` / `shadowed_by` 标明覆盖关系。

### 4.2 写入

```http
POST   /api/mcp?scope=global
POST   /api/mcp?scope=agent&agent_id=coder
POST   /api/mcp?scope=project&workspace=E:/repo
PATCH  /api/mcp/{name}?scope=...
DELETE /api/mcp/{name}?scope=...
```

请求体为一个 MCP server 定义。非法 scope、缺失上下文、空名称、同层重复名称或配置结构非法时返回 4xx；响应返回脱敏后的该层 CatalogItem。

## 5. 验收标准

- [ ] AC1：全局、Agent、项目各配一个同名 MCP 时，`view=sources` 可看到三项，`view=effective` 只显示项目项。
- [ ] AC2：无论 MCP 是否尚未连接，`GET /api/mcp` 都能看到已配置服务器；不能再返回空的死状态集合。
- [ ] AC3：同 Agent 的两个 Session 绑定不同项目 MCP 时，两个 ToolView 分别包含各自项目工具，互不污染。
- [ ] AC4：全局配置外部修改后由 ConfigService watcher 触发重载；不再启动 MCP 私有文件轮询任务。
- [ ] AC5：POST/PATCH/DELETE 分别只改目标全局/Agent/项目文件；凭据在所有 HTTP 响应中被脱敏。
- [ ] AC6：禁用、无效和连接失败配置均可见、状态正确，其他合法服务器照常工作。
- [ ] AC7：后端 MCP、ToolService、Config/Workspace/AgentProfile 契约测试，`pytest -q`、`ruff check src tests` 和 `git diff --check` 通过。

## 6. 测试计划

- 单元：三层 resolve、覆盖、禁用、无效配置、脱敏、全局连接复用、Agent/Session scope 覆盖、restriction 隔离。
- 契约：HTTP 查询与 CRUD、必填上下文、ConfigService watcher、AgentProfile/Workspace 写入边界。
- 集成：两个 Session 同 Agent 不同 workspace 的 ToolView；全局配置更新后工具重载。
- 配对客户端：按 Ftre C2 验证真实当前 Session 的 Agent/Workspace 查询与 CRUD。

## 7. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-09-02 | 初始定稿 | 旧 C2 的双层配置和诊断 API 已与现行 MCP/UI 架构脱节，需要独立阶段收口三层 Owner 与协议。 |
