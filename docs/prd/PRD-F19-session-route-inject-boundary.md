# PRD-F19：Session HTTP 路由 Plugin 化与 Inject 边界收敛

| 字段 | 值 |
|---|---|
| 阶段 | F19 |
| 名称 | Session HTTP 路由 Plugin 化与 Inject 边界收敛 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-24 |
| 定稿日期 | 2026-08-24 |
| 验收日期 | 2026-08-24 |
| 关联文档 | `docs/TODO.yaml`、`AGENTS.md`、F9/F14/F18 PRD |

## 1. 背景与目标

`src/ftre/services/session/plugin.py` 当前同时创建 Session Service、SessionEvent Service
和 HTTP 路由。路由需要 `agents`、`inbox`，但 Session Provider 为避免依赖循环使用
`lambda: ctx.get(..., strict=False)` 延迟查找。这是隐式 Service Locator，导致 Provider 的
`inject` 声明与真实消费者不一致，也让 Session 路由无法单独卸载或审计。

目标：新增独立 `session-routes` Builtin Plugin，通过显式 `inject` 获取
`sessions`、`agents`、`inbox`、`http`；Session Provider 只拥有 Session/SessionEvent 和
持久化生命周期，保持 `/api/sessions*`、`/api/workspaces` 等现有 HTTP 行为不变。

## 2. 范围

### 功能需求

- [x] FR1：新增 `src/ftre/plugins/builtin/session_routes/plugin.py`，声明
  `inject = ("sessions", "agents", "inbox", "http")`，注册 Session HTTP Router。
- [x] FR2：从 `src/ftre/services/session/plugin.py` 删除 `http` 注入、路由构造和
  `ctx.get("agents")`/`ctx.get("inbox")` 延迟查找；Provider 只创建 `sessions`、
  `session_events` 并绑定 Service close。
- [x] FR3：`services/session/router.py` 的 `build_router` 接收稳定的 Service 实例，删除
  `current()` 和 callable accessor；路由行为、路径、响应结构保持不变。
- [x] FR4：Composition 声明 required `session-routes` Plugin，并保证它在 `sessions`、
  `agents`、`inbox`、`http` 就绪后加载。
- [x] FR5：路由 Plugin 的 Router disposer 绑定当前 Fiber；unload/restart 只影响 Session
  路由，不关闭 Session、Agent 或 Inbox Service。
- [x] FR6：补充架构、启动和生命周期测试，禁止 Session Provider 使用跨 Service 的动态
  `ctx.get`，并验证真实 Composition 路由仍完整存在。

### 非目标

- 不修改客户端 wire protocol、Session Service 数据模型、Agent Core、cordis-py 或用户数据。
- 不新增 SessionPort、RouterCoordinator、Service Bag 或全局 setter。

## 3. 技术方案

```text
sessions Plugin
  ├─ provide("sessions")
  ├─ provide("session_events")
  └─ 持有 repository / close 生命周期

session-routes Plugin
  ├─ inject("sessions", "agents", "inbox", "http")
  └─ 注册 /api/sessions*、/api/workspaces 路由
```

Session Route Plugin 的 `owner` 仍使用 `sessions`，不改变 HTTP 诊断和客户端路径；Plugin
id 使用 `session-routes`，让生命周期 Owner 在 Composition 中可见。

## 4. 验收标准

- [x] AC1：Session Provider 的 `inject` 不包含 `http`，生产代码不再有
  `ctx.get("agents")`/`ctx.get("inbox")` 路由回调。
- [x] AC2：`session-routes` Plugin 的依赖声明与真实构造一致，入口可解析，Router Effect
  可逆，unload/restart 不产生重复路由。
- [x] AC3：默认 Composition 的 `/api/sessions`、`/api/sessions/search`、
  `/api/sessions/{id}/messages`、`/api/workspaces`、`/api/health` 等路由回归通过。
- [x] AC4：全量 pytest、architecture/contracts/startup/lifecycle、ruff、diff check 和
  Gateway health smoke 通过；生成缓存和空目录清零。

## 5. 测试计划

- AST/文本架构门禁：Session Provider 不持有 HTTP Router，不通过动态 Service Locator 取得
  Agent/Inbox；新 Plugin 显式 inject。
- 生命周期：session-routes unload/restart 后路由快照只增删对应 Owner，Session Service 仍 ACTIVE。
- 启动/集成：真实 Composition 路由快照和 FastAPI materialization 保持现有路径。

## 6. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-24 | 初版：拆出 Session HTTP Route Plugin，消除 Session Provider 的动态跨 Service 查找 | refactor-cleanup-audit 发现 Provider 的 `ctx.get("agents"/"inbox")` 绕过 Inject 边界 |
| 2026-08-24 | 完成 F19.1–F19.4；FR1–FR6、AC1–AC4 全部验收 | 504 项测试、F19 专项、Host wheel、Gateway smoke、ruff、diff 和工程卫生门禁通过 |
