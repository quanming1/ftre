# Changelog

## [未发布]

### F1 后端插件化架构重构

- 基于 `cordis` 公共 Context/Fiber/Service/Inject/Effect 建立唯一新 Composition Root。
- 新增 `app / platform / services / features` 四层后端目录与显式 Plugin Manifest/Discovery/Manager。
- Config、Filesystem、Workspace、HTTP、Session、Agent、Tool、Prompt、MCP、Skill、Schedule、Team 等能力改为公开 Service/Plugin 边界。
- Gateway CLI 改为薄转发；保留既有 SessionLane、MessageBus、WebSocket 与 Octo 兼容表面。
- 新增 scoped registry、生命周期、外部插件启用隔离、路由基线和合成第三方 Plugin 契约测试。

### F2 核心数据面 Service 化迁移

- Session、Agent Runtime、Bus、Channel、Command 和内置 Tool 的真实实现迁入 `services` Owner，旧路径降级为兼容 shim。
- 新增 AgentRuntimeProvider、Session/Agent/Trace/Attachment/Command Router，WebSocket 复用 Composition Host。
- 生产启动路径移除 aggregate API、全局 setter 和 `bind_legacy_api`；保留旧 Plugin Kernel/API 仅作为兼容测试面。

### F3 旧 Plugin Kernel 与兼容入口退役

- Hook/Event、SessionTitle 和 Builtin Plugin 测试统一到 Cordis Context、Feature Plugin 与 Service-owned Router。
- 删除旧 `ftre.plugin.kernel`、`ftre.plugin.builtin`、`ftre.plugin.api` 和 aggregate API；新增架构导入门禁。
- 删除 `ftre.plugin` 窄兼容入口、`LegacyPluginContext` 和旧 `setup`/`module.Class` Plugin 解析路径；Plugin 统一使用 Cordis `apply` 契约。

### F4 架构债务清理与单一 Owner 收敛

- 删除 Agent、Session、Bus、Channel、Command、Tool 旧路径转发壳和迁移测试入口。
- Config、Trace、MCP、Gateway Process、Attachment 实现归入各自 Service/Feature/App Owner。
- 删除 Feature 通配符转发、HTTP legacy/compat 注册 API、死依赖和外部旧插件测试入口。
- 生产代码与测试统一使用新四层导入路径，新增 F4 架构门禁。
- Cordis Plugin apply 失败时自动回滚已注册 Effect，避免启动半成品泄漏。

### F5 Schedule Owner 收敛与调度生命周期治理

- 将 CronStore、ScheduleService、CronScheduler、CronChannel 和 cron Tool 收敛到
  `features/schedule`，删除 `services/tools/builtin/cron.py` 与 Schedule 空壳。
- Schedule Plugin 接管 Channel/Tool 注册、Scheduler 启停和 Effect 清理，Bootstrap 不再手工
  装配 CronScheduler。
- 新增 Store 安全、Service CRUD、Scheduler 并发、Plugin 生命周期、Router 边界和旧实现删除
  架构测试；全量 327 项测试通过。

## [0.2.4] - 2026-08-20

### 修复

- B2：`context_compact_start` 事件明确携带实际使用的摘要模型，客户端不再将普通对话模型误显示在压缩横幅。
- E1：会话搜索覆盖推理、工具参数和工具结果等可见文本，并提供 offset 分页，避免正文命中较多时旧会话被静默遗漏。

### 性能

- B2：`/compress-fast` 改用批量原子消息更新，避免多条消息逐条重写完整 session state；24 MB 生产会话副本实测耗时由 979ms 降至 454ms。
