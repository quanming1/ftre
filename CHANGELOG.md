# Changelog

## [未发布]

### F8 Command Plane 与 Agent Plane 解耦

- CommandRuntime 收敛为 `CommandContext + CommandResult(success/error)`，记录配对的
  `command/run` 与 `command/done` 生命周期事件。
- 普通 Command 在 SessionLane 内直接执行，不再进入 Mailbox/TurnExecutor；`/compact`、
  `/compress-fast`、`/fork` 改用 CompactionPort/SessionService，`/allow`、`/deny` 复用
  既有确认事件恢复 Agent。
- 删除 TurnExecutor 的 Command 状态机、混合结果类型、完整 AgentLoop 闭包和重复命令适配器。

### F9 Service Inject/Provide 与架构债务清理

- AgentLoopProvider、TurnExecutor、WebSocket、MCP、Schedule 和内置 Tool 的跨 Service 依赖
  改为显式 Inject/Provide 或类型化 Provider 参数。
- 删除 Loop Service Locator、动态依赖查找、重复 facade 与无效生命周期回调；Attachment、
  Session、Command、Compaction 等 Owner 统一到公开 Service/Contract。
- 新增 Service 依赖图和架构门禁，覆盖 Owner、注入声明、生命周期可逆性和旧路径扫描。
- 审计复核统一 Tool 的 `sessions` 注入键，并为 Session Title 后台线程增加可逆关闭和
  Fiber disposer。

### F7 Agent Core Hook 直连与 Turn-stopping Continuation

- 将 Core-facing HookSpec、Payload、Result 的唯一实现下沉到 `ftre-agent-core`，ftre 业务路径改为重导出。
- Agent Core 直接注入 Cordis `HookRuntime` 和 Agent scope context，Tool/LLM 不再经过转换适配器。
- 删除 `ToolHookBridge`、`HookedToolRegistry`、`HookedLLMAdapter` 及空的 `infrastructure/agent_core` 目录。
- `agent/turn-stopping` 在 finalize 前支持有界 `ContinueTurn`，`agent/turn-stopped` 保持 ftre 侧完成后观察语义。
- 新增 Core direct pipeline 与 ftre 架构门禁；ftre 389 项 pytest、Core 234 项 pytest、双仓库 ruff 和 Gateway smoke 通过。

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

### F6.9 Command 解耦与旧 Hook 删除

- CommandService 在 Bus 接入边界完成解析；普通命令由 SessionLane 串行执行，命令文本不进入 Inbox 或模型上下文。
- `/compact` 与 `/compress-fast` 只通过公开 CompactionPort；TurnExecutor 不再匹配或派发 Command。
- 删除 `before_run`、`before_messages_build`、可变 Filter 兼容路径和 `runtime/hooks.py`，统一使用结构化 Prompt Hook。
- 新增 Command ingress 契约与架构门禁；全量 375 项测试、Hook/契约/架构/生命周期专项通过。

### F6.10 生命周期、作用域、并发与故障测试

- HookRuntime 注册绑定 Cordis Fiber Effect；unload/restart 清理旧 Listener，并等待 in-flight Hook 收敛。
- 新增 Agent scope 同 id 重建隔离、pre-step 故障重试、Turn cancellation/retry、压缩失败保留 pending 和去重消费测试。
- 全量测试与专项门禁覆盖 pending 不丢失、不重复执行及生命周期资源无泄漏语义。

### F6.11 最终验收与执行报告

- F6 核心范围完成最终验收：全量测试、Hook/契约/架构/生命周期专项、ruff、YAML、diff check 和 Gateway 启停 smoke 全部通过。
- 补齐 `session/created` / `session/disposed` 生命周期 Hook，完善公共 Hook 清单文档和执行报告。
- F6.12 PyPI 发行物切换继续作为独立后置任务。

## [0.2.4] - 2026-08-20

### 修复

- B2：`context_compact_start` 事件明确携带实际使用的摘要模型，客户端不再将普通对话模型误显示在压缩横幅。
- E1：会话搜索覆盖推理、工具参数和工具结果等可见文本，并提供 offset 分页，避免正文命中较多时旧会话被静默遗漏。

### 性能

- B2：`/compress-fast` 改用批量原子消息更新，避免多条消息逐条重写完整 session state；24 MB 生产会话副本实测耗时由 979ms 降至 454ms。
