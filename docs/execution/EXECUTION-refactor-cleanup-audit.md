# 架构清理审计执行报告

## F7/C1 复审附录（2026-08-21）

本附录是对上文 F1-F6 清理记录的当前状态复核，范围扩展为经用户授权协同验证的
`E:\ftre` 与 `E:\ftre-agent-core` 两个独立仓库；客户端、`E:\cordis-py` 源仓库和用户
运行数据目录不在修改范围。

### Owner 与旧实现核对

| 旧 Owner/入口 | 当前 Owner | 删除/迁移证据 |
|---|---|---|
| Core `FtreCoreHookManager`、`ON_*` | Core `HookDispatcher` + typed `HookSpec` | Core 生产源码 AST 无 ftre/Cordis 反向依赖；旧符号扫描无命中 |
| ftre `ToolHookBridge` / `HookedToolRegistry` | Core `ToolHandler` 直接 dispatch Tool contracts | `src/ftre/infrastructure/agent_core` 不存在；ftre `src` 无桥接引用 |
| ftre `HookedLLMAdapter` | Core `ReasoningExecutor` 直接 dispatch `llm/stream` | 适配器文件和导出删除；Core direct pipeline 测试通过 |
| finalize 后的 stop observation | Core finalize 前 `StopTurn`/`ContinueTurn`，ftre finalize 后 `agent/turn-stopped` | continuation budget、取消和 stop/continue 测试通过 |

### 生命周期复审

| 资源 | 创建/注册 Owner | 停止/回滚证据 |
|---|---|---|
| Cordis Plugin/Fiber | `Composition → PluginManager → PluginLoader` | Fiber dispose/restart、失败回滚和 required/optional 诊断专项通过 |
| Hook Listener | `HookRuntime.register` + `ctx.effect` | unload/restart、in-flight drain、scope 隔离专项通过 |
| AgentLoop/Lane | `AgentLoopProvider` / `SessionLaneRegistry` | stop admission→Lane→compaction→scope；重复 start/stop 语义已补门禁 |
| ChannelManager | `bootstrap` 真实 Host | 重复 start 不重复创建 dispatch task，重复 stop 不重复停止 Channel；新增生命周期回归 |
| Schedule/MCP/Compaction Task | 各 Feature Plugin | `ctx.effect` 绑定 stop/close；既有 schedule、MCP、compaction 生命周期测试通过 |

### 当前最终证据

```text
E:\ftre-agent-core: python -m pytest -q       → 234 passed
E:\ftre:            python -m pytest -q       → 389 passed（含本次 ChannelManager 回归）
两仓库 ruff check（Core .；ftre src tests）   → All checks passed!
两仓库 git diff --check                       → 通过
Core 外部依赖 AST 检查                        → 无 ftre/Cordis 反向依赖
ftre 旧路径 AST 检查                           → 无 ftre.plugin/ftre.agent 等旧 import
Gateway smoke                                    → GATEWAY START OK / GATEWAY CLOSE OK
缓存复核                                        → 两仓库 __pycache__/.pytest_cache/.ruff_cache 均为 0
源码/测试空目录复核                              → 两仓库 src/tests 均为 0
```

活动规范已同步：Core `AGENTS.md`、两仓库 TODO、F7/C1 PRD 的状态/日期/变更记录和
两仓库 CHANGELOG 均已更新。历史 PRD、superpowers 设计稿和已跟踪的 `trace.json` 是
回溯资料，不属于运行入口；用户 `.ftre/` 配置、`data/` 日志和数据库未删除。

## 1. 范围与结论

本轮使用 `refactor-cleanup-audit` 对 `E:\ftre` 及经用户明确授权的
`E:\ftre-agent-core` 协同改动做收尾清理。目标是移除已经没有生产、测试或动态入口
引用的死代码、迁移兼容壳和重复 HTTP 注册 API，并修正文档与当前代码树不一致的问题；
不改变客户端协议、`E:\cordis-py` 源仓库或用户运行数据。

清理结果：已删除 13 个无引用/无效模块或资源，移除 2 个仅为旧 HTTP 聚合 API 服务的转发方法，
同步当前架构说明，并保留仍承担持久化数据兼容或隔离测试职责的 fallback。最终质量
门禁见第 5 节。

## 2. 审计方法

1. 读取仓库协作约束、PRD/TODO、提交规范和本 Skill 的完整流程。
2. 建立生产代码、测试、Composition manifest、动态 `module:attribute` 入口和文档引用
   的交叉检索基线。
3. 对每个删除候选执行反向引用核验；仅当 `src`、`tests`、manifest 和运行时字符串均
   无引用时删除。
4. 删除后先运行架构、启动、生命周期和 Schedule 专项，再运行全量门禁。
5. 最后清理测试/构建产生的缓存，并复核空目录、临时脚本、敏感调试文件和工作区状态。

## 3. 已删除的无引用代码

| 路径 | 结论 |
|---|---|
| `src/ftre/app/cli/logging.py` | 仅转发 `ColorFormatter`，CLI 直接使用 `ftre.main` 实现 |
| `src/ftre/app/gateway/diagnostics.py` | `StartupDiagnostics` 无生产、测试或动态入口引用 |
| `src/ftre/app/gateway/http/health.py` | 健康路由由 `HttpService.register_health()` 唯一拥有 |
| `src/ftre/app/gateway/http/server.py` | 未被 App、Composition 或测试引用；监听生命周期由外部 Gateway Host 管理 |
| `src/ftre/app/gateway/http/server_plugin.py` | 无 manifest/入口引用，属于未启用的旧 Server Plugin |
| `src/ftre/app/gateway/http/service_plugin.py` | 默认清单已使用 `services.http.plugin:apply`，该文件只是旧入口 |
| `src/ftre/plugins/builtin/mcp/private.py` | `private_scope` 无调用方；MCP 私有配置由 Feature Service 处理 |
| `src/ftre/plugins/builtin/skill/store.py` | 仅重导出 `SkillService`，真实 Owner 是 `plugins/builtin/skill/service.py` |
| `src/ftre/services/agent/events.py` | `AgentLifecycleEvent` 无调用方，语义 Hook 已由 `services/agent/hooks.py` 提供 |
| `src/ftre/services/session/compat.py` | `SessionManager` 兼容别名无调用方，旧 Session 入口已退役 |
| `src/ftre/services/system_prompt/base.md` | 只有 HTML 注释，却会被注册进 Prompt；应用基座实际由 `services/agent/config.py` 加载 |
| `src/ftre/services/config/models.py` | `ConfigValue` 无消费者、未公开导出 |
| `src/ftre/plugins/builtin/session_title/config.py` | `TitleConfig` 无消费者；真实配置模型是 `generator.py` 的 `TitleGenConfig` |

同时删除：

- `PluginManager.routers` 旧只读视图；Host 统一消费 `HttpService` 注册表。
- `HttpService.router_objects()` 旧聚合方法；没有正式调用方，避免再次暴露第二套路由 Owner。

## 4. 明确保留的代码

以下命中 `legacy`/`fallback` 的内容经过审计后保留，因为它们不是死代码：

- Session/Trace/JSON 数据格式的读取迁移逻辑，负责已有用户数据恢复。
- `WebSocketChannel` 的隔离测试 Bus fallback，只在未提供完整 Durable Service 的测试场景使用。
- 工具、MCP、附件和进程管理中的异常边界与系统 PATH fallback，属于运行时容错而非模块兼容壳。
- `plugins/builtin/schedule/channel.py`、`store.py` 和 `tool.py`，分别承担 Cron Channel、持久化 Store
  和 Tool factory，均由 `plugins/builtin/schedule/plugin.py` 的动态能力组合使用。

### 4.1 已核验但不在本轮删除范围的债务

| 位置 | 证据 | 处理结论 |
|---|---|---|
| `services/messaging/channel/providers/{websocket,subagent}/plugin.py` | 没有默认 manifest 引用；`bootstrap.py` 在真实 Gateway 路径手工构造 Channel | 不是无引用安全删除项，保留并列为后续“Channel Provider 单一入口”重构 |
| `plugins/builtin/channels/websocket/channel.py` | 真实负责 WS 协议、连接 attach、快照和附件校验，623 行 | 真实 Owner，不作为死代码删除；后续可拆为协议/连接/附件适配子模块 |
| `services/session/service.py`、`persistence/repository.py` | 承担 Session CRUD、Mailbox admission、持久化和生命周期，均被 Runtime/Router 使用 | 真实数据面 Owner；后续按职责拆分，不做机械切文件 |
| `services/tools/builtin/team.py` | Team 工具拥有多条用户可调用行为和 Agent/Session 注入 | 真实行为 Owner；后续可按 Tool family 拆分 |
| `src/ftre/system_prompt.md` | 被 `services/agent/config.py` 唯一加载，内容是实际系统提示词 | 保留；与已删除的无效 `services/system_prompt/base.md` 不是同一资源 |

## 5. 文档与工程卫生

- `AGENTS.md`：移除不存在的本地 `src/cordis` 和 `PluginContext` 描述，改为官方 `cordis.Context`
  与 `services/agent_loop/provider.py`。
- `README.md`：移除“旧包暂时保留”和不存在的 `factory.py`/`tests/plugin` 描述，说明显式插件
  manifest 边界。
- `services/system_prompt/plugin.py`：移除无效 `base.md` 资源加载；Session title Plugin 注释同步到结构化 Prompt Hook。
- `docs/TODO.yaml`：F2/F3/F6 模块路径同步到当前 Owner，修正 F6.11/F6.12 顺序。
- 删除并复核临时调试脚本；不删除用户运行数据目录 `.ftre/`、`data/`。
- 测试夹具 `PausingAgent` 删除未使用的 `reply_id_holder` 参数；保留 async-generator 测试所必需的不可达 `yield`。
- Channel 注释中的旧 `ftre.bus.protocol` 路径已改为当前 `ftre.services.messaging.bus.protocol`。

## 6. 验证记录

删除后专项验证：

```text
python -m pytest -q tests/architecture tests/startup tests/lifecycle tests/plugins/builtin/schedule
69 passed
python -m ruff check --no-cache src tests
All checks passed!
```

孤立资源清理专项：

```text
python -m pytest -q tests/architecture/test_cleanup_no_orphan_resources.py tests/contracts/test_f1_services.py tests/contracts/test_f7_hook_pipeline.py tests/startup/test_composition.py
13 passed
```

最终全量门禁与 Gateway smoke：

```text
python -m pytest -q
385 passed in 45.89s

python -m ruff check --no-cache src tests
All checks passed!

python -m vulture src/ftre --min-confidence 90
无高置信度死代码

python -c "yaml.safe_load(docs/TODO.yaml)"
TODO YAML OK

git diff --check
通过

Gateway smoke: GATEWAY START OK / GATEWAY CLOSE OK

生产源码 stale-reference scan: clean
python -m vulture src/ftre --min-confidence 90: 无高置信度死代码
```

最终生成物复核：`src`/`tests` 下无 `__pycache__` 或 `.pyc`，无空目录，无 `.tmp_*`
调试脚本；`build/`、`.pytest_cache/`、`.ruff_cache/` 和 `src/ftre.egg-info/` 已清理。

## 7. 后置项

- F6.12（cordis-py PyPI 发行物和脱离 `E:\cordis-py` 的洁净安装）仍按 PRD 保持 todo。
- 本报告不执行 commit、push、merge 或跨仓库修改；保留用户当前分支的累计改动供后续分批提交。

## F11 复审附录（2026-08-22）

### 范围与基线

- 仓库：`E:\\ftre`；分支：`feature/F11-compaction-gate-hook`。
- 只审计并修改本仓库；未修改 `E:\\ftre-agent-core`、`E:\\cordis-py`、桌面端或用户运行数据。
- 工作区本来就包含 F11 的累计未提交改动；本轮没有执行 commit、merge 或 push。
- `E:\\ftre-agent-core` 工作区干净；`E:\\cordis-py` 的 `.gitignore` 修改为既有外部状态，本轮未触碰。

### Owner、入口与引用复核

| 检查项 | 结果 |
|---|---|
| 核心压缩 Owner | `src/ftre/services/compaction`、`src/ftre/plugins/builtin/compaction`、`ContextGate` 均不存在 |
| 可选压缩 Owner | `packages/ftre-compaction/src/ftre_compaction` 唯一提供 Service、三条 Hook 和两个命令 |
| Agent 数据面 | `SessionLane` 仅保留 `peek → Hook → claim → Turn → after-turn`，未导入压缩实现 |
| 旧生产 import | AST 复核 `ftre.plugin/agent/session/bus/channel/command/tools/api/config/mcp`：0 条 |
| 重复 Owner/通配导出 | 核心与可选包未发现重复 `provide("compaction")`、通配导入或兼容转发壳 |
| 可选包公开边界 | 将消息估算和 OpenAI 转换提升为 `ftre.services.session.message` 公共导出；可选包不再直接 import `message.converter/token_counter` 私有模块 |

### 生命周期与测试证据

- `ftre-compaction` 的 Service、Hook Receipt 和 Command disposer 均绑定 Cordis Effect；Service
  `close()` 会取消并等待所有 in-flight 压缩 Task，测试覆盖 unload 和重复清理。
- `agent/pre-step` 失败保留队首 pending；`agent/after-turn` 失败不回滚已完成 Turn；overflow
  只有 generation 前进才允许有界 Retry。
- 全量：`python -m pytest -q` → **421 passed**。
- 复审专项：压缩包、架构边界和生命周期 → **47 passed**；此前 F11 架构/契约/生命周期/启动专项
  **149 passed**。
- `python -m ruff check --no-cache src tests packages/ftre-compaction/src packages/ftre-compaction/tests`
  → **All checks passed**。
- `python -m vulture src/ftre packages/ftre-compaction/src --min-confidence 90` → 无高置信度死代码。
- Root 和 `ftre-compaction` wheel/sdist 均构建成功；构建产物随后已清理。

### 文档与生成物收尾

- 修正 `README.md`、`docs/prd/README.md` 中仍把 `ContextGate/CompactManager` 描述为当前 Owner
  的流程图和架构表，统一改为 `agent/pre-step`、`agent/after-turn` 与可选 `ftre-compaction`。
- `docs/TODO.yaml` 解析通过；F11 PRD/TODO/CHANGELOG/执行报告状态一致。
- 最终 `git diff --check` 通过。
- 每次测试/构建后均按绝对路径清理本轮生成的缓存/构建文件；最终复核时源码、测试和 packages
  范围内 `__pycache__`、`.pyc`、`.pytest_cache`、`.ruff_cache`、`.mypy_cache`、`build`、
  `dist`、`*.egg-info` 数量均为 **0**；非 `.git` 空目录为 **0**。

### 仍保留的后置债务

1. `ftre-compaction/plugin.py` 通过 `ctx.get("session_events", strict=False)` 使用可选事件汇，
   但当前 Cordis `inject` 声明没有“可选依赖”语法；这不会阻塞运行，却未被通用 Plugin 注入门禁
   完整表达。后续应冻结 optional-inject 约定，或把 `SessionEventService` 提升为 Composition 默认 Service。
结论：F11 的旧实现、重复 Owner、缓存和当前文档漂移已完成收尾；可选事件汇的 optional
inject 表达仍是独立后置项，不影响本轮代码、生命周期和质量门禁结论。

配置 Owner 的历史债务已在 F11.10 完成，详见下方收尾记录。

### F11.10 配置 Owner 收尾（2026-08-22）

本轮进一步完成了此前定位的压缩配置债务：

- src/ftre/services/agent/config.py 删除 compact_llm 和所有压缩阈值字段；
- ContextConfig 仅保留核心数据面的 mailbox_capacity；
- packages/ftre-compaction/src/ftre_compaction/config.py 成为压缩配置的唯一解析 Owner；
- Hook/Command 在边界读取 ConfigService.snapshot()，Service 使用不可变 CompactionConfig 快照；
- 清理没有实际消费者的历史 consolidation_ratio、idle_compaction 和 silent 配置示例，
  避免继续承诺不存在的行为；
- 新增配置解析、核心 Owner 门禁和包级中文说明测试。

## F12 审计复审（2026-08-23）

### 范围与边界

- 仓库：`E:\ftre`；当前分支：`develop`。
- 只修改 ftre 仓库源码、测试和架构文档；未修改桌面端、`E:\ftre-agent-core`、
  `E:\cordis-py` 或用户 `C:\Users\蒋全明\.ftre` 数据。
- 工作区在审计开始前已经包含 F12 的累计未提交改动和多个用户生成的 GitHub 临时
  文件；本轮不执行 commit、merge、push、release，也不删除这些不在审计范围内的文件。

### Owner、引用和生命周期结论

| 检查项 | 结果 |
|---|---|
| 旧 ftre 数据面包/模块 | `src/ftre/plugin`、`agent`、`session`、`bus`、`channel`、`command`、`tools`、`api`、`config`、`mcp` 均不存在；生产 AST 未发现旧 import |
| Inbox Owner | `packages/ftre-inbox` 唯一拥有 QueueItem、双队列、Repository、worker、claim 和 wire snapshot |
| Agent/Turn 边界 | `AgentService` 只执行 `InboundMessage`；TurnExecutor 的 Inbox 依赖改为 Provider 显式传入，不再从 AgentLoop `getattr` 查找 |
| Plugin 必选依赖 | Inbox Plugin 改用声明的 `sessions`、`agents`、`hook_runtime` 句柄；去除宽松依赖查找 |
| unload/restart | Inbox close 取消 worker、取消 receipt，并清理 agent、Hook Runtime、snapshot/status/before-claim 回调；Hook Runtime 绑定增加 disposer |
| 历史文档 | `AGENTS.md`、`docs/prd/README.md` 明确 F12 当前契约；A/B 的 SessionLane/Mailbox 图标为历史记录，不再作为当前 Owner |

### 本轮修改

1. 修复 `ftre-inbox` unload/restart 的外部引用泄漏，避免旧 Composition、Bus、Session 和
   HookRuntime 被闭包保活。
2. 将 TurnExecutor 的 Inbox runtime capability 改为显式构造依赖，并增加 F9 架构门禁与
   测试夹具声明。
3. 清理当前架构文档中把旧 SessionLane/Mailbox 描述成运行契约的歧义。
4. 盘点并准备清理 `packages/ftre-inbox/build`、`__pycache__` 和测试缓存；不触碰用户临时文件。

### 复审验证

```text
python -m pytest -q packages/ftre-inbox/tests tests/architecture/test_f9_service_injection.py tests/lifecycle/test_f10_lifecycle_faults.py tests/architecture/test_f12_inbox_boundaries.py
→ 37 passed

python -m pytest -q
→ 418 passed（修复测试夹具后）

python -m ruff check --no-cache src tests packages/ftre-inbox/src packages/ftre-inbox/tests packages/ftre-compaction/src packages/ftre-compaction/tests
→ All checks passed

python -m vulture src/ftre packages/ftre-inbox/src packages/ftre-compaction/src --min-confidence 90
→ 无高置信度死代码

python -c "import yaml; yaml.safe_load(open('docs/TODO.yaml', encoding='utf-8')); print('TODO YAML OK')"
→ TODO YAML OK

python -m pip wheel --no-deps --no-cache-dir --wheel-dir E:\ftre\.audit-wheel packages/ftre-inbox
→ ftre_inbox-0.1.0-py3-none-any.whl

Gateway smoke（start_gateway → close）
→ GATEWAY START OK / GATEWAY CLOSE OK

git diff --check
→ 通过
```

### 生成物与最终状态

测试和构建会重新生成缓存；必须在最后一次测试后删除并复核：`__pycache__`、`.pyc`、
`.pytest_cache`、`.ruff_cache`、`build`、`dist` 和 `*.egg-info`。审计结束时源码/测试/包
范围内应为 0 个；当前 Git 工作区仍不干净，这是审计开始前已存在的 F12 累计修改和用户
临时文件造成的，不能通过删除或重置来伪造干净状态。

### 当时复审记录

- F12/C2 当时仍等待独立 Core Step Hook 授权；该项已在后续跨仓库阶段完成，见下方最新复审。
- F6.12 cordis-py PyPI 发行物切换仍按 TODO 保持 todo，属于用户明确后置的独立阶段。

## 最新复审：F12 Inbox + C2 before-reasoning（2026-08-23）

### 范围与边界

- `E:\ftre`：`feature/F12-agent-before-reasoning`，F12 Inbox、Hook 命名、WebSocket
  endpoint、PRD/TODO/执行报告和后端测试。
- `E:\ftre-agent-core`：`feature/C2-agent-before-reasoning`，Core
  `agent/before-reasoning` 契约、ReAct 调用点、测试和版本依赖。
- 未修改桌面端 `E:\binn\ftre-desktop`、`E:\cordis-py` 或用户运行数据；未执行
  commit、push、merge、release。

### Owner 迁移表

| 旧位置/入口 | 新 Owner | 删除/迁移证据 |
|---|---|---|
| `agent/pre-step` / `AgentStepPayload` | ftre `agent/before-turn`：一次 InboundMessage 的 Turn 准入 | 运行时代码与测试已无旧符号；`tests/architecture/test_f6_hook_boundaries.py` 锁定新名称 |
| Core 缺失的 active Step Hook | Core `agent/before-reasoning`：每次 LLM Reasoning 前 | `hooks.py` Spec + `react_runner.py` 调用点；Core/ftre active-steer 集成通过 |
| Session `mailbox.pending` / `SessionLane` | `packages/ftre-inbox`：Repository、双队列、worker、claim、wire snapshot | `tests/architecture/test_f12_inbox_boundaries.py`；旧 mailbox runtime tree 无 Python 文件 |
| SessionService mailbox Owner | SessionService 只拥有 Session/Msg 历史；InboxService 拥有 pending | Service 文档、Repository 迁移测试和唯一 Owner 架构门禁 |

### 生命周期与入口审计

- Composition Root 仍只有 `src/ftre/app/gateway/composition.py`；运行时由
  `bootstrap.py` 统一接线并逆序关闭 AgentLoop、ChannelManager、Composition、Session。
- Inbox Plugin 的 worker、Hook listener、snapshot/status 回调和 Agent 引用均绑定
  close/effect；Inbox close 会取消 worker/receipt 并清空外部回调。重复或已完成的
  `next-turn` request 不再新建无法完成的 receipt，`steer`/`inject` 不创建 Turn receipt。
- AgentLoop shutdown 会关闭 CompletionRegistry，清空 waiter/cache 并给 in-flight waiter
  明确的 `RuntimeError`，避免进程关闭后悬挂协程。
- Core 不持有 Plugin 注册表、QueueItem、Session 或 Inbox；Hook Dispatcher、scope 和
  failure policy 由宿主提供。
- Gateway runtime 启动、取消和清理 smoke 通过；真实 FastAPI WebSocket endpoint 覆盖
  attach、queue/steer prompt、edit、remove、cancel、reconnect。

### 验证与清理证据

```text
ftre:             python -m pytest -q                       -> 425 passed
ftre-agent-core:  python -m pytest -q                       -> 238 passed
两仓库:           python -m ruff check --no-cache ...       -> All checks passed
两仓库:           git diff --check                           -> passed
WebSocket:        tests/startup/test_f12_ws_smoke.py         -> passed
Active steer:     packages/ftre-inbox/tests/test_plugin_hook.py -> passed
```

- 最终搜索：运行时代码中无 `AGENT_PRE_STEP`、`AgentStepPayload`、`ContinueStep`、
  `RejectStep` 或 `agent/pre-step`；历史 PRD/执行记录中的引用均保留在历史说明或变更记录中。
- 测试基线文件已从误导性的 `test_session_lane.py` 重命名为
  `tests/test_inbox_service.py`，内容仍覆盖迁移后的 Inbox 行为。
- Compaction 回归测试和 Inbox README/test 文档已移除当前语境中的旧
  `SessionLane`/`CompactManager` Owner 名称；`legacy_mailbox` 仅保留在一次性迁移边界。
- 已确认并删除仓库根目录未跟踪的 GitHub review 临时文件：`gh-pr*.json`、
  `gh-reviews*.json/txt`、`gr.json`、`pr1505_reviews.json`、`tmp_body.txt`。
- 最后一次测试后清理 `__pycache__`、`.pyc`、`.pytest_cache`、`.ruff_cache`、`build`、
  `dist`、`*.egg-info`；两个仓库剩余数量均为 **0**；源码范围空目录为 **0**。
- 重新构建独立包：`ftre_inbox-0.1.0-py3-none-any.whl` 与
  `ftre_compaction-0.1.0-py3-none-any.whl` 均成功生成。

### 最终状态

- F12 与 C2 PRD：已验收；TODO 阶段和任务：done；CHANGELOG/执行报告已同步。
- F6.12 cordis-py PyPI 发布仍是独立 todo，不属于本轮审计范围。
- 工作区仍不干净：包含此前累计的用户改动和本轮实现，且按仓库规则未擅自提交或
  push；这不是通过删除或 reset 伪造的“干净”状态。
