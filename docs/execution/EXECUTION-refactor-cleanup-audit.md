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

## F18/F19 最新审计复审（2026-08-24）

### 范围与边界

- 仓库：`E:\\ftre`；审计分支：`feature/F19-session-route-inject-boundary`。
- 使用 `refactor-cleanup-audit` 完成 `scope → owner-map → migrate-audit → lifecycle-audit →
  entrypoint-audit → test-audit → artifact-cleanup → final-gates` 闭环。
- 未修改客户端、`E:\\ftre-agent-core`、`E:\\cordis-py` 或用户运行数据；未执行 commit、push、
  merge、release。

### Owner 与引用结论

| 审计项 | 结论 | 证据 |
|---|---|---|
| Inbox | 只拥有 InboxService、Queue Hook、Worker、Repository 和持久化 | `packages/ftre-inbox/src/ftre_inbox/plugin.py` 无业务 Tool 工厂/Tool 注册 |
| Messaging/Task/Team Tool | 三个独立 Package/Plugin，各自注册并绑定 Tool disposer | F18 架构门禁、Composition manifest、Package wheel |
| Team Profile | `ftre-team` 通过公开 `agent_profiles` Service，不 import Agent Profile 私有 helper | F18/F19 Package import AST 门禁 |
| Session HTTP | `session-routes` 显式 inject `sessions/agents/inbox/http`；Session Provider 不再注册路由 | `tests/architecture/test_f19_session_route_inject.py` |
| 重复 Team Owner | 内存 `src/ftre/plugins/builtin/team` 已删除；Session metadata 是团队关系唯一持久状态 | F18 架构门禁与全量测试 |
| Agent Runtime Inbox relay | `_inbox`、`runtime_context["inbox"]` 和函数级 `Injected("inbox")` 均无生产残留 | F17/F18 架构门禁 |

### 生命周期与入口审计

- `session-routes` Router 注册绑定当前 Fiber；restart 后 Session 路由数量保持不变，unload
  只移除 Session 路由，`sessions`/`agents`/`inbox` Service 保持可用。
- 三个业务 Tool Package 的 unload/restart 只影响各自 Tool；Inbox Provider 需要先卸载依赖
  Package 再重启，避免官方 Cordis 依赖刷新同时发生。
- Inbox、Agent、Channel、Schedule、MCP、Compaction 和 HTTP 资源均沿既有 Provider/Plugin
  Effect 路径关闭；没有发现新的裸后台 Task、全局 setter、Service Bag 或第二 Composition。
- `ftre gateway` smoke：`GET /api/health` → HTTP 200，正常 stop。

### 最终验证

```text
python -m pytest -q                         -> 504 passed in 114.86s
python -m ruff check src tests packages --no-cache -> All checks passed
python -m vulture src/ftre packages/*/src --min-confidence 90 -> 无高置信度死代码
python -m pip wheel --no-deps --no-build-isolation --wheel-dir E:\\tmp\\ftre-f19-audit-wheels .
                                            -> ftre-0.3.0，184 文件，wheel 内容无 tests/pyc/cache
git diff --check                             -> 通过
生成物/空目录复核                         -> 0 / 0
```

### 未完成项与诚实边界

- F15 的 PRD/TODO 仍为 `in_progress`，AC19/AC20 等待 feature push 后的 GitHub Actions 和
  分批提交；本审计没有伪造远程 CI 结果，也没有把 F15 改成已验收。
- F6.12 的 cordis-py PyPI 脱离 sibling checkout 发行仍按原 TODO 保持后置，不属于本轮
  ftre Host 清理。
- 工作树仍包含 F15–F19 累计未提交修改；这是执行前既有现场和本轮实现的真实状态，未通过
  reset/删除用户改动伪造干净状态。

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

## 最新审计：F20 默认 Package 组合（2026-08-24）

### 范围与边界

- 仓库：`E:\\ftre`，分支：`feature/F19-session-route-inject-boundary`。
- 本轮审计覆盖 Host、`packages/`、测试、PRD/TODO/CHANGELOG 和运行生成物；
  未修改客户端 `E:\\binn\\ftre-desktop`、`E:\\ftre-agent-core`、`E:\\cordis-py`。
- 工作区在审计开始前已经包含 F12–F19 累计未提交修改；本轮未执行 reset、checkout、
  commit、push、merge 或 release。

### Owner 与依赖审计

| 能力 | 唯一 Owner | 入口/依赖证据 | 结论 |
|---|---|---|---|
| Inbox | `packages/ftre-inbox` | `ftre_inbox.plugin:apply`；provide `inbox` | ACTIVE，拥有 Repository、worker、Queue Hook |
| Compaction | `packages/ftre-compaction` | `ftre_compaction.plugin:apply`；provide `compaction` | ACTIVE，注册三个 Hook 和两个 Command |
| 消息 Tool | `packages/ftre-messaging` | `ftre_messaging.plugin:apply`；inject `channels/tools/inbox` | ACTIVE，唯一注册 `send_message` |
| Task Tool | `packages/ftre-task` | `ftre_task.plugin:apply`；inject `channels/tools/inbox` | ACTIVE，唯一注册 `task` |
| Team Tool | `packages/ftre-team` | `ftre_team.plugin:apply`；inject `sessions/agents/channels/tools/inbox/agent_profiles` | ACTIVE，唯一注册 `team_*`/`wait_agent` |

静态 AST 盘点未发现重复 Service `provide` key 或重复 Composition Manifest id；默认
Composition 仍是唯一业务装配点。Package 源码未 import Agent Runtime、Session Repository、
Composition 或 Plugin Loader 私有模块。

### 入口与生命周期

- 根 `pyproject.toml` 默认依赖五个 Package；每个 Package 的 `ftre.plugins` entry point
  唯一且与 Composition Manifest 一一对应。
- 默认 Composition 状态：`inbox/compaction/messaging/task/team = ACTIVE`。
- 每个 Package 的 Tool/Hook/Command/Worker 都绑定 Cordis Fiber effect；专项测试覆盖
  unload/restart/close、失败保留 pending 和业务 Package 禁用。
- WebSocket 对 Inbox 的动态解析是为 Inbox restart 后不保留 disposed Service 的显式边界，
  不是第二个 Inbox Owner；该行为由 F17/F19 生命周期测试保护。

### 旧实现与引用扫描

- `src/ftre/agent`、`api`、`bus`、`channel`、`command`、`session`、`tools` 等退役根模块
  不存在；生产 Python AST 未发现旧 `ftre.agent/session/bus/channel/command/tools/api` import。
- `before_run`、`before_messages_build`、`agent/pre-step`、Core 旧 Tool Hook 名称未出现在
  生产代码；测试中的命中只位于“禁止重新引入”的架构断言。
- `vulture --min-confidence 90` 无高置信度死代码。
- 扫描仍命中 67 条 `legacy compatibility boundary reviewed in F1` 的 Ruff 注释；它们是
  异常捕获/Injected 默认值的非功能性历史注释，不是兼容导入或第二 Owner。本轮只移除了
  `ftre-team` 中误导性的 `Coordinator` 层级表述，剩余注释登记为后续文档清理项，未批量
  改写可能影响行号和审计基线的业务文件。

### 验证证据

```text
python -B -m pytest -q
→ 509 passed in 135.77s

python -B -m ruff check src tests packages --no-cache
→ All checks passed

python -B -m vulture src/ftre packages/*/src --min-confidence 90
→ 无输出，退出码 0

PIP_NO_INDEX=1 python -m pip install --no-build-isolation -e E:\\ftre
→ passed；五个本地 Package 均已满足根发行依赖

Gateway smoke：GET http://127.0.0.1:48650/api/commands
→ 200；`/compress-fast`、`/compact` 均为 source=`ftre-compaction`

git diff --check
→ passed
```

### 生成物与最终状态

- 最后一次测试/构建后已清理 `__pycache__`、`.pytest_cache`、`.ruff_cache`、build/dist、
  egg-info 和测试生成的 `.ftre-inbox`；仓库扫描剩余 **0** 个目标目录，源码范围空目录 **0**。
- Gateway 当前以 `48650` 运行；用户配置已显式启用 `compaction`。
- F20 PRD FR1–FR5、AC1–AC6 已勾选并标记 `已验收`；TODO F20/F20.1–F20.4 为 `done`；
  CHANGELOG 和 F20 执行报告已同步。
- 工作区仍不干净，状态来源是审计开始前的 F12–F19 累计修改及本轮 F20 文件；本轮没有
  伪造干净状态，也没有提交。独立 Package 的 PyPI 发布仍是后置发行任务。

## 最新审计：C4/F23/B4 Steering 消息边界（2026-08-24）

### 范围与边界

- 仓库与分支：`E:\ftre-agent-core` / `feature/C4-user-message-boundary`；
  `E:\ftre` / `feature/F23-steering-message-boundary`；
  `E:\binn\ftre-desktop` / `feature/B4-steering-message-projection`。
- 只审计本次三端 `message_id`、Steering、Inbox→Session→Client 交接及其 PRD/TODO/测试；
  未修改 `E:\cordis-py`、用户 Session 数据、其它未授权仓库，也未执行 commit/push。
- 三个工作树在审计前已有本阶段未提交修改；保留现场，不用 reset/checkout 或删除无关改动伪造干净。

### Owner 迁移与引用证据

| 旧 Owner/入口 | 当前 Owner | 审计证据 |
|---|---|---|
| Core 以 `reply_id` 作为 Msg.id | Core `RunState.message_id` + `MessageContext` + Event `message_id` | Core AST/`rg` 无旧 segment 引用；C4 A→U→B 测试通过 |
| ftre Host 人工 segment / `insert_messages_after()` | Core message boundary + `SessionProjection` | `src/`、`packages/`、`tests/` 中旧符号扫描 0；F23 Projection 集成测试通过 |
| 客户端 `reply_segment` / `splitActiveReplyBeforeUserMessage` | Desktop `chat.ts` 按服务端 `message_id` reducer | renderer 源码旧符号扫描 0；A/U/B、乱序、重连测试通过 |
| pending/claim | `packages/ftre-inbox` | `InboxService` 单一 Repository/worker/claim Owner；Agent 只接收 `InboundMessage` |

历史文档中的旧名称均位于明确的历史 PRD/迁移矩阵/负向架构断言中，不是生产入口；
旧 `C4 Cordis 风格插件内核` PRD 与 `design-plugin-kernel.md` 已补充“历史阶段/非当前契约”
标记，且 PRD 状态与 TODO `done/已验收` 对齐。

### 生命周期与入口复核

- Core 无进程级队列、Session 或 Plugin 状态；Runner 只生成/旋转 `message_id`，宿主负责 Hook Scope。
- ftre Inbox 由 Composition Plugin provide，Hook/Worker/Receipt/Session 引用绑定 `ctx.effect`；
  `close` 清理 worker、wake、receipt 和依赖引用，重复关闭为安全 no-op。
- SessionProjection 是唯一 Reply Msg 投影/持久化协调者；`SessionEventService` 先投影再广播，
  `AgentLoop` 不再持有 Inbox 或人工消息重排入口。
- Desktop 只消费 WebSocket `message_id`/`reply_id`；不执行 claim、Reasoning、Tool 或 Session 写入。
- 当前 `rg`/Python AST 检查未发现旧生产 import、重复 segment Owner 或跨仓私有 import。

### 验证证据

```text
Core:    python -m pytest -q                         → 240 passed
ftre:    python -m pytest -q                          → 527 passed
ftre:    python -m ruff check src tests packages     → All checks passed
Desktop: pnpm --filter @ftre/renderer test            → 52 files / 514 passed
Desktop: pnpm exec tsc -p packages/renderer/tsconfig.json --noEmit → passed
Desktop: pnpm --filter @ftre/renderer build          → passed（仅既有 CSS/chunk 警告）
Core:    wheel + 临时 venv 洁净安装                  → clean wheel import ok
三仓库:  git diff --check                             → passed
跨层:    tests/test_f23_core_projection_integration.py → 1 passed
```

### 生成物与最终状态

- 最后一次测试后已清理三仓库本阶段生成的 `__pycache__`、`.pytest_cache`、`.ruff_cache`、
  `.vite`、Core `build` 和临时 wheel/venv；复核目标目录数量均为 **0**。
- 依赖目录、用户数据、Session 数据、既有 `node_modules`/`dist` 未做宽泛删除。
- 三仓库工作树仍非 clean，原因是本阶段源码、测试、PRD、TODO、CHANGELOG 和执行报告均未提交；
  按各仓库 AGENTS 规则，本次审计没有擅自 commit/push。提交拆分和 PR 合入仍是后续显式操作。

### 再审计修复记录（2026-08-24）

- 复核 `ftre-inbox` 的依赖图时确认 `session_events` 是 Plugin 的必需公开 Service；生产入口
  已使用声明式 `ctx.session_events`，不再通过动态 `ctx.get()` 查找。
- 该收紧最初暴露两个只验证 Hook 的最小测试上下文缺失依赖，已在测试 fixture 显式提供
  `session_events=None`，并复跑后端全量回归：`527 passed in 120.84s`。
- 三端最终门禁：Core `240 passed`、ftre `527 passed`、Desktop `517 passed`；Core/ftre
  Ruff 与三仓库 `git diff --check` 全部通过。Desktop 本轮仅运行测试，B4 代码未改动，已有
  TypeScript/build 通过记录继续有效。
- Core 的 Vulture 仅报告 `src/tests/test_tool_decorator.py` 中 4 个用于签名反射断言的测试参数
  （`exact`、`limit`、`encoding`、`config`）；它们不是生产死代码，本轮不改动。
- 2026-08-25 复现并修复 Steering placement 的客户端更新缺口：Desktop 在控制 ACK 成功后
  立即更新本地 `steering` placement，并用本地意图防止旧快照回退。

## 最新审计：F24/B5 Queue Operation Response 收尾（2026-08-25）

### 范围与边界

- `E:\ftre` / `feature/F23-steering-message-boundary`：Inbox wire、WebSocket Channel、F24 PRD/TODO/测试。
- `E:\binn\ftre-desktop` / `feature/B4-steering-message-projection`：renderer Queue Response、B5 PRD/TODO/测试与测试入口。
- 未修改 `E:\ftre-agent-core`、`E:\cordis-py`、Electron 业务主进程、用户 Session 数据；未执行 commit/push/merge。

### Owner / 入口 / 生命周期审计

| 能力 | 唯一 Owner | 证据与结论 |
|---|---|---|
| Inbox 状态与 revision | `packages/ftre-inbox` `InboxRepository`/`InboxService` | Repository 原子递增 revision；`wire_snapshot()` 只转换为公开 `session_id/revision/items` |
| Queue Operation Response | WebSocket Channel `_send_queue_response` | `session.prompt`、edit/remove/steer 成功均走同一包装；错误走 `_reject`；Channel 不读原始文件 |
| Queue 广播 | WebSocket Plugin | 监听 Inbox changed Hook；动态解析仅为支持 Inbox 独立 restart，缺失不 fallback，默认 Composition required 门禁失败 |
| 客户端队列投影 | `ClientSessionProjection` + `applyQueueSnapshot` | 仅按服务端 revision 应用；旧 ACK parser、Steering 本地意图和第二队列状态机不存在 |
| Steering 可变性 | WebSocket Channel | next-step 用户项在 claim 前拒绝 edit/remove；queued 项仍由 Inbox 处理 |

### 旧引用与陈旧文档审计

- ftre/renderer 生产代码无 `_send_admission_ack`、`getMessageAckPayload`、`QueueUpdateResult`、
  `MessageAckPayload`、`consumeDurableAdmissionAck`、`steeringRequests`、`markQueueItemSteering`。
- 根 `AGENTS.md`、桌面 `AGENTS.md`、`docs/prd/README.md` 已同步 revision/Queue Operation Response
  当前契约；F12/F22/B4 历史 PRD 增加 F24/B5 supersession 注记，历史 ACK 示例不再被当作现行协议。
- `value.accepted` 生产命中仅位于不修改 Inbox 的 `session.cancel` 控制 ACK；不属于 admission 成功路径。
- `ctx.get("inbox")` 是 WebSocket restart 边界的明确动态解析例外，已在代码注释和本报告登记；其他必选
  依赖仍通过 inject 或 Service Provider 声明。
- `vulture --min-confidence 90` 无生产高置信度死代码；现有 `legacy compatibility boundary reviewed in F1`
  仅是历史 Ruff 注释/异常边界，未被误判为兼容入口。

### 额外清理修复

- 服务端补齐 Steering 只读不变量，避免客户端禁用按钮可被旧/恶意 WS 帧绕过。
- renderer 测试脚本先构建 `@ftre/shared`、`@ftre/ui`，解决清洁工作树下 workspace package `dist` 不存在导致
  12 个测试套件无法解析 `@ftre/ui` 的工程卫生问题。

### 最终验证

```text
ftre:     python -m pytest -q                                  → 531 passed
ftre:     python -m ruff check src tests packages --no-cache   → All checks passed
ftre:     python -m vulture ... --min-confidence 90           → 无输出，退出码 0
desktop:  pnpm test                                            → 517 renderer + 10 platform tests passed
desktop:  pnpm exec tsc -p packages/renderer/tsconfig.json --noEmit → passed
desktop:  pnpm --filter @ftre/renderer build                    → passed（仅既有 CSS/chunk 警告）
both:     git diff --check                                     → passed
```

### 生成物与工作树

- 最终构建/测试后仅清理仓库源码范围内的缓存和自有构建输出；跳过 `node_modules`、`release`、`.git`、用户
  Session/Inbox 数据。源码范围目标目录复核为 0。
- 两个工作树仍有 F22/F23、B3/B4 及本次 F24/B5 的未提交修改；这是既有现场和本次审计变更，未擅自 reset、
  commit 或伪造 clean 状态。
