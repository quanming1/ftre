# F14 执行报告：轻内核 + Plugin-first 最终目标架构

## 状态

| 项目 | 值 |
|---|---|
| 阶段 | F14 |
| PRD | `docs/prd/PRD-F14-final-plugin-first-architecture.md` |
| 当前状态 | 开发中 |
| 分支 | `feature/F14-final-plugin-first-architecture` |
| 范围 | `E:\ftre` 后端及仓库内 `packages/` |
| 外部仓库 | Desktop、`E:\ftre-agent-core`、`E:\cordis-py` 只读，不修改 |

本报告只记录已经由代码、测试或命令证明的事实。未完成项不能因为 PRD 有目标树就标记完成。

## F14.1 基线（2026-08-24）

### 分支与工作树接管

- 原始工作分支为 `feature/F12-agent-before-reasoning`，存在 F12/F13 累计未提交改动。
- 已创建 `feature/F14-final-plugin-first-architecture`，未回滚、覆盖或删除原有改动。
- F14 PRD 已由草稿推进为开发中，TODO F14 保持 `in_progress`。
- 当前改动将作为 F14 继承基线逐项审计；在完成归属审计前不将其声明为 F14 迁移完成。

### 当前 Composition Manifest Owner

| Plugin id | 当前 entry | 类型 | provide/inject 事实 |
|---|---|---|---|
| `config` | `ftre.services.config.plugin:apply` | Host Provider | `config` / `http` |
| `filesystem` | `ftre.services.filesystem.plugin:apply` | Host Provider | `filesystem` |
| `http-service` | `ftre.services.http.plugin:apply` | Host Provider | `http` |
| `system-prompt` | `ftre.services.system_prompt.plugin:apply` | Host Provider | `system_prompt` |
| `message-bus` | `ftre.services.messaging.bus.plugin:apply` | Host Provider | `message_bus` |
| `tools` | `ftre.services.tools.plugin:apply` | Host Provider | `tools` |
| `agent-profiles` | `ftre.services.agent.profile.plugin:apply` | Host Provider | `agent_profiles` |
| `agents` | `ftre.services.agent.plugin:apply` | Host Provider | `agents` |
| `sessions` | `ftre.services.session.plugin:apply` | Host Provider | `sessions`, `session_events` |
| `commands` | `ftre.plugins.builtin.command.plugin:apply` | Host Provider（待 F14.5 归位） | `commands` |
| `workspaces` | `ftre.services.workspace.plugin:apply` | Host Provider | `workspaces` |
| `channels` | `ftre.services.messaging.channel.plugin:apply` | Registry Provider | `channels` |
| `attachments` | `ftre.services.attachment.plugin:apply` | Host Provider | `attachments` |
| `traces` | `ftre.plugins.builtin.trace.plugin:apply` | Optional behavior（待 F14.5 归位） | `traces` |
| `agent-runtime` | `ftre.services.agent_loop.plugin:apply` | Runtime Provider（待 F14.3 合并） | `agent_runtime` |
| `inbox` | `ftre_inbox.plugin:apply` | Optional Package | Inbox capability |
| `subagent-channel` | `ftre.plugins.builtin.channels.subagent.plugin:apply` | Adapter Plugin（待 F14.5 归位） | none |
| `websocket-channel` | `ftre.plugins.builtin.channels.websocket.plugin:apply` | Adapter Plugin（待 F14.5 归位） | none |
| `skill` | `ftre.plugins.builtin.skill.plugin:apply` | Behavior Plugin（待 F14.5 归位） | `skills` |
| `mcp` | `ftre.plugins.builtin.mcp.plugin:apply` | Behavior Plugin（待 F14.5 归位） | `mcp` |
| `plan` | `ftre.plugins.builtin.plan.plugin:apply` | Behavior Plugin（待 F14.5 归位） | none |
| `team` | `ftre.plugins.builtin.team.plugin:apply` | Behavior Plugin（待 F14.5 归位） | `teams` |
| `schedule` | `ftre.plugins.builtin.schedule.plugin:apply` | Behavior Plugin（待 F14.5 归位） | `schedule` |
| `context-govern` | `ftre.plugins.builtin.context_govern.plugin:apply` | Behavior Plugin（待 F14.5 归位） | none |
| `session-title` | `ftre.plugins.builtin.session_title.plugin:apply` | Behavior Plugin（待 F14.5 归位） | none |

证据来源：`src/ftre/app/gateway/composition.py` 的 `default_manifests()`，以及各 entry
模块顶部的 `inject`/`provide` 声明。

### 当前目标映射

| 当前路径/能力 | F14 目标 | Owner | 计划批次 |
|---|---|---|---|
| `src/ftre/kernel/hooks` | `src/ftre/kernel/hooks` | Kernel mechanism | F14.2 |
| `src/ftre/kernel/plugins` | `src/ftre/kernel/plugins` | Plugin Runtime | F14.2 |
| `src/ftre/services/agent_loop` | `src/ftre/services/agent/runtime` | Agent Provider | F14.3 |
| `src/ftre/plugins/builtin/*` | `src/ftre/plugins/builtin/*` | 各行为 Plugin | F14.5 |
| `plugins/builtin/command` | `plugins/builtin/command` | Command Plugin | F14.4/F14.5 |
| `plugins/builtin/trace` | `plugins/builtin/trace` | Trace Plugin | F14.5 |
| `services/messaging/channel/providers/*` | `plugins/builtin/channels/*` | concrete Channel Plugin | F14.5 |
| `packages/ftre-inbox` | 保持独立 Package | Inbox Plugin/Service | F14.7 |
| `packages/ftre-compaction` | 保持独立 Package | Compaction Plugin/Service | F14.7 |

### Hook Owner 基线

| Hook 语义 | 当前定义 | F14 目标 Owner | 当前状态 |
|---|---|---|---|
| `agent/*` | `services/agent/hooks.py` | Agent Service | 已归位 |
| `tool/*` | `services/tools/hooks.py` | Tool Service | 已归位 |
| `system-prompt/*` | `services/system_prompt/hooks.py` | Prompt Service | 已归位 |
| `session/*` | `services/session/hooks.py` | Session Service | 已归位 |
| `inbox/*` | `ftre_inbox/hooks.py` | Inbox Package | 已归位 |
| Compaction listeners | `ftre_compaction/hooks.py` | Compaction Package | 已归位 |
| 通用 dispatch/runtime | `platform/hooks/*` | `kernel/hooks/*` | 待 F14.2 |

### 已识别架构债务

| 债务 | 证据 | Owner | 清理批次 |
|---|---|---|---|
| `platform` 名称仍承载 Kernel 机制 | `src/ftre/kernel/` | Kernel | F14.2 |
| `features` 名称隐藏 Plugin 生命周期 | `src/ftre/plugins/builtin/*` | Builtin Plugins | F14.5 |
| `agent_runtime` 与 `agents` 双 Service/Provider | `composition.py`、`services/agent_loop/plugin.py` | Agent | F14.3 |
| AgentLoop 仍拥有 Bus 分流、Command/Inbox binding | `services/agent_loop/runtime/loop/engine.py` | Messaging/Agent | F14.3/F14.4 |
| concrete Channel 深嵌在 Service 目录 | `services/messaging/channel/providers/*` | Channel Plugin | F14.5 |
| Command/Trace Service 仍位于 Host services | `plugins/builtin/command`、`plugins/builtin/trace` | Builtin Plugins | F14.5 |
| Package 仍引用旧 `ftre.kernel` 测试/入口 | `packages/ftre-inbox`、`ftre-compaction` | Package | F14.2/F14.7 |
| 多处 `ctx.get(..., strict=False)` 作为可选依赖查找 | 各 Provider Plugin | 各 Owner | F14.6 |
| 多处 `bind_*` 生命周期桥接 | Session、Command、Inbox、AgentLoop | 各 Owner | F14.3-F14.6 |

### F14.1 新增基线门禁

新增 `tests/architecture/test_f14_baseline.py`，当前验证：

- 默认 Manifest id 与 entry 唯一且可解析；
- Builtin Provider `provide` key 不重复；
- Plugin inject/provide 元数据可静态读取；
- 当前机制层不 import 产品 Service/Package；
- Package 不反向 import Host 私有 Runtime/Repository；
- 退役 Port/ServiceBag/AgentControlPort 等入口不回归。

## 当前验证

```text
python -m pytest -q
→ 439 passed in 125.13s
```

F14.1 的架构专项、ruff、diff check 和新增门禁已在基线提交前执行：

```text
python -m pytest -q tests/architecture/test_f14_baseline.py tests/architecture/test_f13_plugin_first.py tests/architecture/test_import_boundaries.py tests/architecture/test_f9_service_injection.py
→ 26 passed in 4.75s

python -m pytest -q
→ 439 passed in 125.13s

python -m ruff check --no-cache src tests packages/ftre-inbox packages/ftre-compaction
→ All checks passed

git diff --check -- <F14 基线文件>
→ passed
```

基线提交：`5501fc1 chore(agent): 固化 F12 F13 迁移基线`。

F14.1 已完成；后续迁移仍必须以本报告记录的债务为输入，不得把债务清单当作已修复证据。

## F14.2 Kernel 命名与业务零知识迁移（2026-08-24）

### 已完成

- `src/ftre/platform` 已整体迁移为 `src/ftre/kernel`；`plugin_runtime` 已迁移为
  `kernel/plugins`，生产代码、测试和当前架构文档统一使用新路径。
- 删除 `src/ftre/kernel/hooks/names.py`；Kernel Hook 导出现在只包含 Runtime、Spec、Scope、
  Receipt 和 Diagnostics 机制，不再导出 Agent/Session/Tool/Prompt 业务名称或 `PUBLIC_HOOK_NAMES`。
- Agent Hook 名称由 `services/agent/hooks.py` 持有；Session、System Prompt、LLM、Tool Hook
  名称分别由对应 Owner 或 `ftre-agent-core` 持有；测试从语义 Owner 读取 Spec 名称。
- Kernel README 已改为机制边界说明，不再维护业务 Hook 名称总表。
- Package、Gateway、Service 和测试的 `ftre.platform` import 已全部切换为 `ftre.kernel`。

### 验证

```text
python -m pytest -q tests/architecture/test_f14_baseline.py tests/architecture/test_f13_plugin_first.py tests/architecture/test_f3_no_legacy_imports.py tests/architecture/test_f3_plugin_loader.py tests/architecture/test_f6_hook_boundaries.py tests/hooks/test_hook_runtime.py tests/contracts/test_f7_hook_pipeline.py
→ 48 passed in 5.37s

python -m pytest -q
→ 445 passed in 115.67s

python -m ruff check --no-cache src tests packages/ftre-inbox packages/ftre-compaction
→ All checks passed
```

### 仍待后续批次处理

- `services/agent_loop`、`features` 和 concrete Channel 目录仍按 F14.3-F14.5 迁移；
- 历史阶段文档中仍有 `platform/features` 旧树描述，F14.9 统一审计后清理；
- F14.2 没有引入兼容 alias 或第二 Kernel 实现。

提交将在 F14.2 的代码与文档完成后按职责分批创建。

## F14.3 Agent Runtime 内聚（2026-08-24）

### 已完成

- `services/agent_loop` 已删除；Loop、Driver、TurnExecutor、CompletionRegistry 和 Runtime
  provider 归入 `services/agent/runtime/`，不再形成顶层 Service 目录。
- `services/agent/plugin.py` 是唯一 Agent Provider：只 provide `agents`，在同一 Fiber 内
  创建 AgentService 和私有 Runtime，并以一个 Effect 负责停止/解绑。
- 删除 `agent-runtime` Manifest、`agent_runtime` Service、`AgentRuntimeService`、
  `AgentLoopProvider` 公共入口和 Bootstrap 的 Runtime 句柄；AgentService 仍只公开
  `InboundMessage → TurnOutcome`、active 状态和取消。
- SessionProjection 归还 SessionService；Agent Runtime 不再创建 Projection，WebSocket 只
  消费 SessionService 的 projection capability。

## F14.4 Messaging Ingress 与 Command/Inbox 交接（2026-08-24）

### 已完成

- 新增 `services/messaging/bus/ingress.py`，由 Messaging Owner 定义
  `messaging/inbound` HookSpec，并复用原有 `IngressResult` ACK 语义。
- MessageBusService 成为唯一 inbound consumer，拥有 request/reply resolve、错误唤醒和关闭；
  Agent Runtime 不再订阅 Bus、不解析 Command、不绑定 Inbox。
- Command Plugin 在 inbound Hook 中旁路执行 slash command；纯 Command 不创建 Turn、不进入
  Inbox；结果继续使用现有 typed Session command envelope。
- Inbox Package 在同一 Hook 中接管未消费的普通输入和 cancel，并通过 AgentService 交付
  `InboundMessage`；未安装 Inbox 时由 MessageBus 返回稳定 capability error。
- 删除 AgentLoop 的 `_consume`、Command parser/dispatcher、Inbox binding 和 Command result
  publisher；同步更新 WS、Command、Inbox、Session 路由和生命周期测试。

### 验证

```text
python -m pytest -q tests/contracts/test_f9_command_ingress.py tests/lifecycle/test_agent_runtime_plugin.py tests/lifecycle/test_f10_lifecycle_faults.py tests/contracts/test_f2_runtime_provider.py tests/architecture/test_f6_agent_layer.py tests/architecture/test_f9_command_boundaries.py tests/architecture/test_f13_plugin_first.py tests/startup/test_composition.py
→ 33 passed in 69.18s

python -m pytest -q
→ 445 passed in 114.69s

python -m ruff check --no-cache src tests packages/ftre-inbox packages/ftre-compaction
→ All checks passed
```

F14.3/F14.4 已完成；F14.5 继续处理 Builtin Plugin 目录和 concrete Channel 归位。

## F14.5 Builtin Plugin 目录与 Owner 迁移（2026-08-24）

### 已完成

- `src/ftre/features` 已删除，Skill、MCP、Plan、Team、Schedule、Context Govern 统一迁入
  `src/ftre/plugins/builtin/`。
- Command Service/Plugin、Trace Service/Plugin、Session Title Plugin 迁入各自 Builtin Plugin；
  Command/Trace 的稳定 Service key 保持不变，但 Python 私有路径不再伪装成 Host Service。
- concrete WebSocket/Subagent Channel 迁入
  `plugins/builtin/channels/{websocket,subagent}`；`services/messaging/channel` 只保留
  Channel registry/base contract。
- `tests/features` 迁入 `tests/plugins`；所有生产/测试/Package import 和 Composition entry
  已同步，旧目录、空目录和 Provider 路径删除。
- 新增 `plugins/README.md`、`plugins/__init__.py` 和 `plugins/builtin/__init__.py`，明确
  Builtin Plugin 的 Owner、Effect 和可选行为边界。

### 验证

```text
python -m pytest -q tests/startup/test_composition.py tests/architecture/test_f14_baseline.py tests/architecture/test_import_boundaries.py tests/architecture/test_f6_hook_boundaries.py tests/architecture/test_f9_service_injection.py tests/architecture/test_f5_schedule_owner.py tests/architecture/test_f13_plugin_first.py tests/plugins tests/test_mcp.py tests/test_trace_store.py tests/test_title_gen.py
→ 77 passed in 5.13s

python -m pytest -q tests/architecture/test_f14_baseline.py tests/architecture/test_f2_http_owner.py tests/architecture/test_f4_no_legacy_packages.py tests/architecture/test_import_boundaries.py tests/architecture/test_f5_schedule_owner.py tests/architecture/test_f6_hook_boundaries.py tests/plugins
→ 36 passed in 5.10s

python -m ruff check --no-cache src tests packages/ftre-inbox packages/ftre-compaction
→ All checks passed
```

F14.5 已完成；F14.6/F14.7 继续审计 Host Service 依赖和独立 Package 发行门禁。

## F14.6/F14.7 Host Service 与 Package 发行边界（2026-08-24）

### Host Service Owner 审计

以下表格由 `default_manifests()`、各 Provider 的字面量 `provide`/`inject` 和真实
实现路径反推，并由 `tests/architecture/test_f14_baseline.py` 的唯一 Owner 门禁锁定：

| Service key | 唯一 Provider | 真实实现 | inject/生命周期结论 |
|---|---|---|---|
| `config` | `services.config.plugin` | `ConfigService` + `config/store.py` | `http`；路由 disposer 绑定 Fiber |
| `filesystem` | `services.filesystem.plugin` | `LocalFilesystemService` + `policy.py` | 无必选依赖；路径策略只在 Owner 内 |
| `http` | `services.http.plugin` | `HttpService` | 无必选依赖；health 注册随 Fiber 清理 |
| `system_prompt` | `services.system_prompt.plugin` | `SystemPromptService` | 无必选依赖；行为由 Plugin 注册 |
| `message_bus` | `services.messaging.bus.plugin` | `MessageBusService` + `EventBus` | `hook_runtime`；inbound consumer 由 effect 关闭 |
| `tools` | `services.tools.plugin` | `ToolService` + `ToolRegistry` | 无必选依赖；贡献由各 Plugin effect 清理 |
| `agent_profiles` | `services.agent.profile.plugin` | `AgentProfileService` + `AgentManager` | `http`；profile 路由 disposer 随 Fiber 清理 |
| `sessions` | `services.session.plugin` | `SessionService` + 私有 Repository/Projection | `hook_runtime,http,message_bus`；Session 自己发布 lifecycle/flush Hook |
| `agents` | `services.agent.plugin` | `AgentService` + `services/agent/runtime` 私有实现 | `agent_profiles,sessions,message_bus,channels,tools,system_prompt,hook_runtime,plugin_manager`；Runtime 与 Service 同 Fiber 关闭 |
| `channels` | `services.messaging.channel.plugin` | `ChannelService` + `ChannelManager` | `message_bus`；Manager stop effect 归 registry Owner |
| `workspaces` | `services.workspace.plugin` | `WorkspaceService` | `sessions`；不复制 Session 状态 |
| `attachments` | `services.attachment.plugin` | `AttachmentService` | `http`；附件路由 disposer 随 Fiber 清理 |

LLM 没有虚构的 `llm` Service key：请求/stream 由 `ftre-agent-core` 适配，Agent Runtime
消费 `AgentConfig`。PRD 5.2 已同步为“外部 LLM 适配，无新增单实现 Host Service”。
Command/Trace 虽然提供 `commands`/`traces` key，但它们是 `plugins/builtin` 的行为/控制面
Plugin，不冒充 Host Service；具体实现分别见 `plugins/builtin/command` 和 `trace`。

审计发现并已修复的债务：

- `SessionService` 不再暴露 `bind_flush_dispatcher`/`bind_lifecycle_dispatcher`；构造时注入
  `HookRuntime`，直接发布 `session/flush`、`session/created`、`session/disposed`。
- `CommandRuntime` 生命周期 sink 在构造时传入；删除 `bind_lifecycle` setter，Command Plugin
  不再运行时回填回调。
- `InboxService` 的 Agent、Hook Runtime 和 before-claim 策略在构造时注入；删除
  `attach_agent`、`bind_*` 以及 WebSocket 快照/status 回调。队列事实只发 `inbox/*` Hook。
- `SessionEventService` 在 Session Provider 中一次性注入 Projection、MessageBus、Hook Runtime，
  自己完成“投影 → Hook → outbound”顺序；Agent Provider 不再绑定/解绑 Loop emitter。
- Compaction Package 的 `session_events`、`inbox` 改为显式 `inject`，删除事件 sink setter 和
  缺失 Inbox 的 no-op/fallback 分支。未安装/未启用 Package 时由 Plugin 状态保持缺失，不污染 Host。

仍保留的 `ctx.get(..., strict=False)` 只有两类：Composition `initial_services` 覆盖 Provider
默认实例，或 Session/WS/Agent Runtime 的可选能力晚绑定（这些能力存在依赖环，不能伪造必选
`inject`）。Command、Compaction 和 Package 内部的必选依赖均已改为显式 `inject`/属性读取。

### Package 发行门禁

`ftre` 的 `pyproject.toml` 现在提供 `inbox`、`compaction`、`full` extras；主包依赖列表不含
两个可选发行物。两个 Package 均具备完整 build-system、版本、README、唯一
`ftre.plugins` entry point，并声明其公开宿主契约依赖：

| Package | entry point | 关键依赖 | 独立结果 |
|---|---|---|---|
| `ftre-inbox==0.1.0` | `inbox = ftre_inbox.plugin:apply` | `ftre>=0.2.6`、`cordis-py==0.4.0`、`ftre-agent-core>=0.1.2` | wheel 无缓存/测试/Host 私有源码；无包 Host 仍可启动 |
| `ftre-compaction==0.1.0` | `compaction = ftre_compaction.plugin:apply` | Inbox + ftre 公共 Service/Hook + core | 安装后可 discovery/load/restart/unload；不安装时 Host 不出现该候选 |

Plugin Discovery 读取已安装的 `ftre.plugins` 元数据但延迟 import；内置同名 Manifest 优先，
避免重复 id。新增 `test_f14_package_boundaries.py` 覆盖 extras、entry point、Host 私有 import、
生成物和 discovery 延迟导入；新增 Host Service key/provider 唯一性与 callback setter 门禁。

隔离验证（临时目录在仓库外，完成后清理）：

```text
python -m pip wheel --no-deps --no-cache-dir . packages/ftre-inbox packages/ftre-compaction --wheel-dir E:\ftre-f14-package-wheel
→ ftre-0.2.6、ftre_inbox-0.1.0、ftre_compaction-0.1.0 全部构建成功
wheel 内容检查 → 12 files/个；无 __pycache__、.pyc、数据库、tests；均有 entry_points.txt
洁净 venv + 仅 Host → inbox=FAILED/entry_import_failed（可选缺失），agents=ACTIVE，AgentService 可用
洁净 venv + 两个 wheel → inbox=ACTIVE、compaction=ACTIVE；compaction restart=True、unload=True，卸载后 Service=None
```

### 包化候选审计

MCP、Skill、Schedule、Team 均已是 `plugins/builtin/*` 边界完整的 Builtin Plugin，但本批均为
`not-ready`：仍依赖 Host 配置/Tool/Session/Filesystem 具体实现，缺少独立 pyproject、发行物
entry point、洁净安装和独立复用场景。按 PRD 4.5 不创建空 Package 壳；后续若出现独立发布或
按需安装价值，单独阶段逐项通过七条门禁。

### F14.6/F14.7 验证

```text
python -m pytest -q tests/contracts/test_f7_hook_pipeline.py tests/startup/test_composition.py tests/lifecycle/test_agent_runtime_plugin.py tests/architecture/test_f13_plugin_first.py packages/ftre-inbox/tests packages/ftre-compaction/tests
→ 76 passed
python -m pytest -q tests/architecture/test_f14_package_boundaries.py tests/architecture/test_f14_baseline.py
→ 12 passed
python -m ruff check --no-cache src tests packages
→ All checks passed
```

F14.6/F14.7 已完成；下一批输入是 F14.8 的生命周期、故障、最小 Composition 和 Package
restart/in-flight Hook 组合验证。

## F14.8 生命周期、故障与最小 Composition（2026-08-24）

新增 `tests/lifecycle/test_f14_lifecycle_matrix.py`：

- 关闭 Inbox、MCP、Skill、Plan、Schedule、Team、Session Title 后，Host 仍可建立真实
  Composition，`agents` ACTIVE，缺失 Inbox 不会生成 fallback Service；Composition.close 可重复调用。
- Session Title Plugin unload 后，其 Hook listener 完成 dispose，Agent Service 和其他基础 Service
  仍可用；Inbox 的 restart/unload、Worker cancel、pending 恢复由 F10 lifecycle tests 覆盖。
- Plugin Loader 的 required failure、missing dependency/PENDING、Fiber effect LIFO 和 Context
  dispose 幂等由既有 architecture/lifecycle tests 覆盖；Command 旁路、Agent Turn、Hook 取消和
  Package restart 由 contracts/Package tests 与隔离 venv smoke 覆盖。

本批专项结果：

```text
python -m pytest -q tests/lifecycle/test_f14_lifecycle_matrix.py
→ 2 passed
python -m pytest -q
→ 453 passed in 113.65s
python -m ruff check --no-cache src tests packages
→ All checks passed
```

F14.8 已完成；F14.9 进入旧路径、陈旧文档、生成物和空目录的最终审计。

## F14.9 旧路径、死代码与生成物清理（2026-08-24）

### 清理结果

- `src/ftre` 生产代码、Package 源码和测试中旧 `platform/features/services.agent_loop` import、
  `ServiceBag`、`AgentControlPort`、`CompactionPort`、callback setter 和第二 Agent Runtime key
  均为 0；保留在历史 PRD/执行记录和负向架构断言中的文字是审计证据，不是运行时引用。
- `README.md`、`README.zh-CN.md`、`AGENTS.md`、`services/README.md`、Kernel/App/Plugin README
  已统一使用 Kernel / Service / Builtin Plugin / Package 术语；中文 README 的旧目录树、
  `SessionLane/MailboxStore/ContextGate/CompactManager` 数据流已删除。
- 本批涉及的 Compaction Package 旧 F1 行内注释已改为当前事件出口原因；Package 的 no-op
  Hook fallback 和 Service callback setter 已删除。其他历史执行记录中的注释只作为审计证据，
  不属于运行时入口。
- 测试/构建后生成的 `__pycache__`、`.pyc`、Package `build/`、`.pytest_cache`、`.ruff_cache`
  只在最终验证后清理；`data/sessions.db` 属于执行前已有的用户运行数据，保留不纳入提交。

### 最终源树快照

```text
src/ftre/{app,kernel,services,plugins}
packages/{ftre-inbox,ftre-compaction}
tests/{architecture,contracts,lifecycle,startup,plugins}
```

F14.9 已完成；F14.10 只剩最终文档状态、全量门禁复跑、Gateway smoke 和干净工作树核对。

## F14.10 最终验收（待最后门禁）

本报告在最后一次测试、缓存清理和提交后更新 AC 逐条证据、提交列表、Gateway smoke 输出和
最终 `git status --short`。在此之前 PRD/TODO 阶段仍保持 `in_progress`，避免用中间状态宣称终局完成。

## 后续批次精确输入

- F14.2：将 `platform` 迁为 `kernel`，并把 Package/业务 Hook 的 import 改到 Owner；
- F14.3：合并 `agent_runtime`/`AgentLoop` 为 `services/agent/runtime` 私有实现；
- F14.4：移除 AgentLoop inbound 分流，建立 Messaging inbound Hook；
- F14.5：逐个迁移 `features`、Command、Trace、concrete Channel 到 `plugins/builtin`；
- F14.6/F14.7：收紧 Service inject 并完成 Package clean-install 门禁。
