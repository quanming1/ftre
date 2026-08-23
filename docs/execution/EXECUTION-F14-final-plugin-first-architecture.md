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
| `commands` | `ftre.services.command.plugin:apply` | Host Provider（待 F14.5 归位） | `commands` |
| `workspaces` | `ftre.services.workspace.plugin:apply` | Host Provider | `workspaces` |
| `channels` | `ftre.services.messaging.channel.plugin:apply` | Registry Provider | `channels` |
| `attachments` | `ftre.services.attachment.plugin:apply` | Host Provider | `attachments` |
| `traces` | `ftre.services.observability.trace.plugin:apply` | Optional behavior（待 F14.5 归位） | `traces` |
| `agent-runtime` | `ftre.services.agent_loop.plugin:apply` | Runtime Provider（待 F14.3 合并） | `agent_runtime` |
| `inbox` | `ftre_inbox.plugin:apply` | Optional Package | Inbox capability |
| `subagent-channel` | `ftre.services.messaging.channel.providers.subagent.plugin:apply` | Adapter Plugin（待 F14.5 归位） | none |
| `websocket-channel` | `ftre.services.messaging.channel.providers.websocket.plugin:apply` | Adapter Plugin（待 F14.5 归位） | none |
| `skill` | `ftre.features.skill.plugin:apply` | Behavior Plugin（待 F14.5 归位） | `skills` |
| `mcp` | `ftre.features.mcp.plugin:apply` | Behavior Plugin（待 F14.5 归位） | `mcp` |
| `plan` | `ftre.features.plan.plugin:apply` | Behavior Plugin（待 F14.5 归位） | none |
| `team` | `ftre.features.team.plugin:apply` | Behavior Plugin（待 F14.5 归位） | `teams` |
| `schedule` | `ftre.features.schedule.plugin:apply` | Behavior Plugin（待 F14.5 归位） | `schedule` |
| `context-govern` | `ftre.features.context_govern.plugin:apply` | Behavior Plugin（待 F14.5 归位） | none |
| `session-title` | `ftre.services.session.title.plugin:apply` | Behavior Plugin（待 F14.5 归位） | none |

证据来源：`src/ftre/app/gateway/composition.py` 的 `default_manifests()`，以及各 entry
模块顶部的 `inject`/`provide` 声明。

### 当前目标映射

| 当前路径/能力 | F14 目标 | Owner | 计划批次 |
|---|---|---|---|
| `src/ftre/kernel/hooks` | `src/ftre/kernel/hooks` | Kernel mechanism | F14.2 |
| `src/ftre/kernel/plugins` | `src/ftre/kernel/plugins` | Plugin Runtime | F14.2 |
| `src/ftre/services/agent_loop` | `src/ftre/services/agent/runtime` | Agent Provider | F14.3 |
| `src/ftre/features/*` | `src/ftre/plugins/builtin/*` | 各行为 Plugin | F14.5 |
| `services/command` | `plugins/builtin/command` | Command Plugin | F14.4/F14.5 |
| `services/observability/trace` | `plugins/builtin/trace` | Trace Plugin | F14.5 |
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
| `features` 名称隐藏 Plugin 生命周期 | `src/ftre/features/*` | Builtin Plugins | F14.5 |
| `agent_runtime` 与 `agents` 双 Service/Provider | `composition.py`、`services/agent_loop/plugin.py` | Agent | F14.3 |
| AgentLoop 仍拥有 Bus 分流、Command/Inbox binding | `services/agent_loop/runtime/loop/engine.py` | Messaging/Agent | F14.3/F14.4 |
| concrete Channel 深嵌在 Service 目录 | `services/messaging/channel/providers/*` | Channel Plugin | F14.5 |
| Command/Trace Service 仍位于 Host services | `services/command`、`services/observability/trace` | Builtin Plugins | F14.5 |
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

## 后续批次精确输入

- F14.2：将 `platform` 迁为 `kernel`，并把 Package/业务 Hook 的 import 改到 Owner；
- F14.3：合并 `agent_runtime`/`AgentLoop` 为 `services/agent/runtime` 私有实现；
- F14.4：移除 AgentLoop inbound 分流，建立 Messaging inbound Hook；
- F14.5：逐个迁移 `features`、Command、Trace、concrete Channel 到 `plugins/builtin`；
- F14.6/F14.7：收紧 Service inject 并完成 Package clean-install 门禁。
