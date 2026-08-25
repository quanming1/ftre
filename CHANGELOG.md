# Changelog

### F29 LLM Stream Fallback Plugin（已完成，未发布）

- 新增 `ftre-llm-fallback` Package：仅在最后一次 Core attempt、主模型尚无任何流式输出且
  错误码命中配置时调用备用模型；前序失败仍由 Core Retry 处理。
- ConfigService 新增公开 `resolve_llm(provider, model)` 快照解析；取消、overflow、部分输出、
  未知错误和备用失败均不递归 fallback。

### F28 LLM Error Recovery Plugin（已完成，未发布）

- 新增 `ftre-llm-recovery` 可选 Package，消费 Core `llm/error`，按错误码配置 retry/stop
  和退避建议；Core 仍唯一拥有重试执行器。
- 默认安装但可配置禁用；overflow/context_length 继续由 `ftre-compaction` 处理，Plugin
  支持 Fiber restart/unload 且不残留 listener。

### F27 Compaction 用户消息确定性生成（开发中）

- `all_user_messages` 改由 `ftre-compaction` 按 Msg 快照代码生成，LLM 不再承担机械性复述。
- 默认 `chunkTokens` 调整为 200000，显式配置仍可覆盖。

### F26 Compaction 按 token 分块摘要（开发中）

- 压缩内容默认按约 100k token、保持 Msg 边界切块，每个 chunk 只交给一个 LLM，避免多个
  Worker 重复处理完整 Session；chunk 并发数、超时、重试和 token 上限均可配置。
- 分块摘要按原始顺序本地确定性合并，保留现有 `state_snapshot`、Hook、Session 和 WebSocket 协议。

### F25 Compaction 三路并行摘要（已完成，未发布）

- `ftre-compaction` 对同一 Session 快照并行运行 intent/technical/continuity 三个摘要 Worker，
  本地确定性合并为一个 `state_snapshot`，不改变 Hook、Session 或 WebSocket 协议。
- Worker 支持独立重试、超时和统一失败回退；只有三路全部成功才发出唯一
  `context_compact_done`，避免半成品摘要污染历史。

### F24 Queue Operation Response（已完成，未发布）

- `session.prompt` 与 `session.updateQueue` 成功后统一返回 `type=session/queue`，将
  `request_id`、操作结果和 Inbox `revision/items` 放进同一个响应；取消指令仍保留独立控制 ACK。
- 删除 WebSocket admission ACK 的 `value.accepted` 成功路径；原始 `inbox.json`、`next_turn`、
  `next_step` 不进入 wire payload，重复 request_id 继续由 Inbox 幂等处理。
- 配套 Core C4 已发布为 `ftre-agent-core==0.2.1`，CI 与跨仓安装锁定该消息边界版本。

### F23 Steering 消息边界（已完成，未发布）

- Inbox 在 `agent/before-reasoning` 安全边界执行 `checkpoint → UserMessage 落库/广播 → claim`，
  失败时 pending 保留，重试复用稳定 UserMessage id。
- SessionProjection 改按 Core `message_id` 聚合，同一 `reply_id` 下自然持久化
  `Assistant A → UserMessage → Assistant B`；删除 Host 的 segment、重排 API 和 capability flag。
- Desktop B4 只按服务端 `message_id` 投影，USER_MESSAGE 与 queue snapshot 乱序时保持无空窗。

### F22 Runtime Steering（已完成，未发布）

- `session.prompt` 的 `mode=queue|steer` 在 WebSocket、Bus 和 Inbox 间完整保真；非法 mode
  明确拒绝，旧客户端缺省仍按 queue。
- Steering 在 `agent/before-reasoning` Hook 中 DB-first 持久化并广播 `USER_MESSAGE`，再
  claim Inbox；Session 写入失败时 pending 保留，重试使用稳定 message id 幂等。
- 客户端队列横幅新增“插入当前运行”按钮，服务端 placement 切换为 steering 后等待
  `USER_MESSAGE` 交接，不创建第二条消息，也不产生消失→出现的视觉空窗。

## [0.3.0] - 2026-08-24

### F21 Command 接入异步化（已完成，未发布）

- 修复 `/compact` 等慢命令占用全局 MessageBus inbound consumer，导致普通消息长时间停留
  在客户端本地队列的问题。
- Command Plugin 现在先返回接纳 ACK，再由 CommandService 后台执行命令；同一 request_id
  在执行中或成功完成后不会重复执行，卸载时会取消后台任务。

### F20 默认 Package 安装与装配（已完成，未发布）

- `ftre` 默认发行依赖纳入 `packages/` 下的 `ftre-inbox`、`ftre-compaction`、
  `ftre-messaging`、`ftre-task`、`ftre-team`；原有 extras 保留为裁剪安装兼容入口。
- Composition 默认声明并装配五个 Package Plugin；压缩命令 `/compact`、`/compress-fast`
  在默认 `/api/commands` 列表中可见，业务 Package 仍可通过配置禁用。

### F19 Session Route Inject 边界收敛（已完成，未发布）

- 新增 `session-routes` Plugin；Session Provider 只拥有 `sessions`/`session_events`，不再
  通过 `ctx.get("agents"/"inbox")` 迟查跨 Service 依赖。
- Session HTTP 路由改为显式 Inject、独立 Fiber Effect 和可逆 unload/restart；补充路由
  生命周期门禁与三个业务 Tool Package 的最小行为回归。

### F18 Tool Package 边界收敛（已完成，未发布）

- 新增 `ftre-messaging`、`ftre-task`、`ftre-team` 三个独立 Tool Package，分别拥有
  `send_message`、`task` 和 `team_*`/`wait_agent`；它们通过 `inject("inbox")` 使用队列，
  不再由 Inbox Plugin 注册。
- `ftre-inbox` 收窄为 Inbox Service、Queue Hook、Worker 和持久化 Owner；清理未被实际工具
  消费的重复内存 TeamService。

### F17 Inbox 基础 Owner 收敛（已完成，F18 纠偏）

- `ftre-inbox` 在当前 Gateway Composition 中改为必选 Plugin，只拥有 durable queue、
  admission/claim Hook、Worker 和持久化。
- 删除 Agent Runtime 的 `_inbox` 和 `runtime_context["inbox"]` 死透传；AgentService 继续只处理
  `InboundMessage`；Inbox 队列状态归 Inbox Plugin，业务 Tool 生命周期由各自 F18 Package 管理。
- F16 的无 Inbox 启动结果保留为历史验收快照；当前部署需安装 `ftre[inbox]`。

### F16/C3 Core Hook 面终局收敛（本地已验收）

- Core Tool Hook 从四段收敛为 `tool/before`、`tool/after`，删除 around `tools/execute` 和
  观察 `tools/result`；`agent/turn-stopping` 改为 `agent/stop-decision`，Core 版本提升到 0.2.0。
- ftre Host、Inbox、Compaction 和测试迁移到新协议，全系统公共 Hook 从 17 项收敛为 15 项；
  ftre 版本提升到 0.3.0，两个可选 Package 提升到 0.2.0。
- 未安装 Inbox 时不登记失败的幽灵 Plugin；安装后通过 entry point 发现，Package 的
  load/restart/unload、洁净 venv 和 Gateway/WebSocket smoke 均通过。
- 两仓全量测试 485/238 passed，Core/Host/Package wheel 无测试、缓存和旧 Hook 引用。
- Windows CLI 后台 Gateway 输出改用 ASCII 状态标记，避免 GBK 控制台编码异常。

### F15 Hook 语义收敛（本地预验收，CI 待触发）

- 将 ftre Host Hook 从 22 项收敛为 10 项，全系统公共 Hook 固定为 17 项；Core 7 项契约冻结。
- 删除 Agent lifecycle/request/turn-stopped、Session event/flush 和 Inbox 三种细粒度 mutation Hook；
  `messaging/inbound` 改为 `messaging/route`。
- 关键 Session/Inbox 状态通知改为 awaited PARALLEL；HookRuntime 统一 Context/Fiber 生命周期，
  Plugin 不再重复注册 receipt disposer。
- 补齐 in-flight unload、队列恢复、Package wheel/洁净安装和 Gateway smoke；全量测试 486 passed。
  F15 GitHub Actions 尚未触发，正式验收待 feature push 后完成。

### F14 轻内核 + Plugin-first（已验收）

- F14.6 收敛 Host Service：Session/Command/Inbox/SessionEvent/Compaction 改为构造注入或公开
  Hook，删除运行时 callback setter；Agent Runtime 继续只处理已接纳的 `InboundMessage`。
- F14.7 为 `ftre-inbox`、`ftre-compaction` 增加独立 wheel/entry point 和 Host `inbox`、
  `compaction`、`full` extras；Plugin discovery 支持安装发行物，未安装可选包时 Host 仍能启动。
- F14.8 增加无可选包的最小 Composition 与 Builtin Plugin unload 生命周期回归覆盖。
- 收尾审计删除 WebSocket 临时 Host App、Agent Runtime 的重复 Session 事件出口和 Inbox Hook fallback；补充 claim 失败保留 pending 的生命周期门禁。

### F13 Plugin-first 内核收敛与消息交接（已验收）

- Composition/Bootstrap 不再手工创建业务 Service 或注册业务 HTTP 路由；Agent Runtime、
  WebSocket、Subagent 和各 Service/Feature 路由均由 Provider Plugin 通过 Inject/Effect
  装配和清理。
- Agent Runtime 在 Inbox claim 后、TurnExecutor/LLM 前完成一次正式 UserMessage 历史交接，
  沿用 request_id 相关性；TurnExecutor 不再持有普通输入持久化 Owner。
- 保持 `ftre-inbox` 可选：禁用时 Agent Runtime 仍可组合并返回稳定 capability error；新增
  Owner、路由、重启/卸载、无 Inbox 和公共 Channel 名称架构测试。
- ftre 全量测试 439 passed；Desktop `pnpm test`（renderer 488 + platform 7）通过；
  独立 renderer `tsc --noEmit` 的既有测试夹具缺少 `awaitingEcho`，未在本后端阶段修改。

### F12 独立 Inbox Package 与权威队列协议（已验收）

- 新增 `packages/ftre-inbox`：独立双队列、原子 JSON 持久化、旧
  `mailbox.pending` 一次性迁移、`followup/steer/inject`、Worker、Queue Hook 和
  权威 `session/queue` 投影。
- `AgentService` 收敛为 `run(InboundMessage)`；旧 SessionLane、MailboxStore、
  Session mailbox API、旧快照 payload、queue position 和 `frame_id` 输出别名已移除。
- WebSocket 使用 `session.prompt`、`session.updateQueue`、`session.cancel` 与独立
  `session/status`；Command 保持 Agent Plane 旁路。
- `ftre-agent-core>=0.1.2` 已提供 `agent/before-reasoning`，运行中 steer 可在下一次
  Reasoning 前作为普通消息进入 Core 上下文；Core wheel/PyPI 发布仍按 F6.12 单独安排。
- 审计补充：重复或已完成的 request 不再创建无法完成的 receipt；AgentLoop shutdown 会
  关闭 CompletionRegistry 并唤醒 waiter；Inbox、Compaction 测试和文档中的旧 Owner 名称已清理。
- 修复执行中删除 Session 的生命周期竞态：删除前等待 active Turn 完整取消和消息投影收尾，
  最终 Reply 持久化失败时保留投影快照，并跳过已删除 Session 的空通道状态事件。

## [0.2.6] - 2026-08-22

### BUG 修复（2026-08-22）

- 修复 Cron Scheduler 通过 `MessageBusService` 投递时的门面调用回归，并补充真实 Service
  注入测试。
- 修复 Agent 私有 MCP 配置未进入运行时的问题：Turn 前按 Agent profile 建立连接、注册
  scoped 工具视图；公共连接复用，禁用配置隔离，卸载时清理连接和工具。
- 修复工作区 `.gitignore` 为 GBK/非 UTF-8 编码时无法追加 `.ftre/` 的问题，写回保留原编码
  和换行风格。

### F11 上下文压缩门控 Hook 化

- 新增 `agent/after-turn` 控制型 Hook；SessionLane 固定为
  `peek → pre-step → claim → Turn → after-turn`，压缩不再进入 Lane/ContextGate 实现。
- 删除核心 ContextGate 与 compaction Service/Feature 目录；保留通用 maintenance 状态桥，
  pending、blocked、取消和客户端 `compacting` 协议不变。
- 新增可独立构建的 `packages/ftre-compaction`，集中提供 CompactionService、三条 Hook、
 `/compact` 和 `/compress-fast`；未启用时核心 Gateway 正常运行且命令稳定返回不可用。
- 压缩配置迁入 ftre-compaction.config，核心 AgentConfig 不再拥有压缩阈值或摘要模型；
  清理无消费者的历史配置示例，并为独立包补充中文架构与生命周期说明。

## [0.2.5] - 2026-08-22

### Gateway CORS 修复

- 默认允许 `localhost`/`127.0.0.1` 的桌面开发端口跨域访问 Gateway API；自定义 CORS origins 仍按精确值匹配。

### F10 Compaction Service Owner 收敛

- 将 `CompactionService` 从 `features/compaction` 迁入 `services/compaction`，由 Service
  Plugin 唯一创建并提供 `compaction` key。
- 删除 `CompactionPort`、旧 `contracts.py` 和 Feature 层实现；Compaction Feature 只
  注册 `agent/pre-step` 与 `agent/request-error` Hook。
- AgentLoop、ContextGate、Command、Provider 直接使用 `CompactionService`，压缩行为和
  客户端协议保持不变。

### F8 Command Plane 与 Agent Plane 解耦

- CommandRuntime 收敛为 `CommandContext + CommandResult(success/error)`，记录配对的
  `command/run` 与 `command/done` 生命周期事件。
- 普通 Command 在 SessionLane 内直接执行，不再进入 Mailbox/TurnExecutor；`/compact`、
  `/compress-fast`、`/fork` 改用 CompactionService/SessionService，`/allow`、`/deny` 复用
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
- `/compact` 与 `/compress-fast` 只通过公开 CompactionService；TurnExecutor 不再匹配或派发 Command。
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
