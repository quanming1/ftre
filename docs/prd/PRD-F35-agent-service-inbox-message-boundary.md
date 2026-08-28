# PRD-F35 Agent Service、Inbox Hook 与 UserMessage 边界

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F35 |
| 名称 | Agent Service、Agent Runtime、Agent Profile 与 Inbox 边界收敛 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-27 |
| 定稿日期 | 2026-08-27 |
| 验收日期 | 2026-08-27 |
| 关联文档 | `docs/TODO.yaml` F35；`docs/prd/PRD-F30-llm-service-package.md`；`docs/prd/PRD-F33-agent-package-final-architecture.md`；`docs/prd/PRD-F34-tool-service-runtime.md`；`AGENTS.md` |

> **当前执行状态：F35 全部完成。** F35.1～F35.6 均已按“实现 → 验证 → commit”闭环完成；最终提交为 `feat(F35): 完成 Agent 与 Inbox 边界收敛`。

## 1. 背景与目标

### 1.1 背景

F30 已完成统一 LLM Service、Provider、Retry、Fallback 和 LLM Hook；F33 已完成 `ftre-agent` 与 `ftre-agent-runtime` 的 Package 方向；F34 已完成 ToolService 的唯一 Owner。当前仍有四类边界问题：

1. `ftre-agent-runtime` 同时创建并暴露 `agents` Service，又负责 AgentLoop 实现。Runtime 变成了公共 Facade 的 Owner，违背“`ftre-agent` 是门面、Runtime 是内部实现”的蓝图。
2. `src/ftre/services/agent` 这个目录仍让人误以为存在第二个 Agent Service Owner；Agent Profile、配置解析、路由和 Agent 运行时职责没有完全分开。
3. Inbox 通过 `InboundMessage` 与 Agent 耦合，且 busy 判断、claim、run 不是一个清晰的原子边界。重复运行时可能先 claim 后失败，造成队列项目丢失或产生失败的幽灵消息。
4. Core 的 `ContinueTurn` 只是继续执行的控制信号和隐藏 Hint，并不是用户消息。压缩、steer 或 Inbox 注入消息时，客户端需要看到真实的 `UserMessageEvent`，同时结束/收起前一个 Assistant 消息，再创建下一段 Assistant 消息。

### 1.2 目标

建立唯一、可测试、可独立演进的 Agent Service 边界：`ftre-agent` 暴露稳定 Agent API，`ftre-agent-runtime` 只注册并实现运行工厂，`AgentProfileService` 负责配置/提示词/工具策略解析，`ftre-inbox` 负责排队和准入；在每个 React 边界以严格事件顺序注入真实 UserMessage，并让 Session、Agent 内存和客户端使用同一条消息事实。

### 1.3 非目标

- 不修改 `E:\ftre-agent-core` 的 ReAct 算法、Hook 类型、`ContinueTurn` 语义或 Core Tool 协议；复用现有 Core 合约。
- 不重新设计 F30 的 LLM Retry/Fallback，也不把 Retry/Fallback 逻辑复制到 Inbox 或 Agent Service；`llm/error` 仍由 LLM Service 管理，`agent/run-error` 只处理 Agent 级生命周期。
- 不把 Inbox、Channel、Session Repository、MCP、Workspace、具体 ToolRegistry 或配置文件解析塞进 `ftre-agent`。
- 不让 Agent 了解 `InboundMessage`、队列项目、Channel 或 UI。
- 不在本阶段改客户端布局；客户端只消费已有/增强后的事件流，具体 UI 由客户端阶段处理。
- 不保留旧 Agent Owner、兼容壳、重复 Registry 或“先实现再兼容”的尾巴。

### 1.4 已冻结的前置决策

以下结论来自前置阶段，F35 只消费它们，不重新发明第二套实现：

| 前置能力 | 已冻结结论 | F35 的使用方式 |
|---|---|---|
| LLM Service | Provider/Adapter 统一 `responses` 与 `chat completions`；每次 `llm/stream` 负责一次调用流包装；失败进入 `llm/error`，先完成规定次数 Retry，再按策略 Fallback | Runtime 只调用 LLM Service；Agent/Inbox 不捕获并复制 LLM Retry/Fallback |
| Context Compaction | 按内容 token 数切 chunk，默认 `200K`，参数可配置；每个 chunk 记录大小、序号、模型和耗时；用户消息列表由代码生成，不要求 LLM 重复输出 | Compaction 作为 Agent Runtime 的外部能力，不改变 UserMessageEvent 边界 |
| ToolService | ToolService 是唯一工具 Owner，公开 `get/schemas/execute` 及取消、超时、错误和作用域投影 | Runtime 只注入 ToolService，Profile 只提供 tool policy |
| 客户端消息元数据 | 用户/AI 消息下的复制、token、模型、时间等信息采用 hover + `visibility`，不能通过条件渲染改变消息高度 | UserMessageEvent 必须及时到达，客户端无需刷新即可更新边界 |

这些前置决策不属于 F35 的重做范围；如果实现发现它们与本 PRD 冲突，必须先更新依赖 PRD，而不是在 Agent Service 内增加旁路逻辑。

## 2. 需求范围

### 2.0 分阶段执行规则

F35 按以下顺序推进，每个阶段都是独立的“实现 → 验证 → commit”闭环：

| 阶段 | 只做什么 | 阶段验证 | 阶段完成后的 commit |
|---|---|---|---|
| **F35.1（已完成）** | `ftre-agent` 提供唯一 `agents` Service；Runtime 删除 Service 构造/provide，改为注册 Factory；固定 Composition 顺序 | Owner 架构扫描、Plugin 启动/关闭测试、现有 run/cancel/status 回归、`pytest`、`ruff`、`git diff --check` | `feat(F35): 分离 AgentService Owner 与 Runtime Factory` |
| **F35.2（已完成）** | 冻结 Agent API、状态、Event、Hook 和错误契约；Runtime 通过 Handle 适配现有 Loop | Contract/clean import/fake factory 测试 | `feat(F35): 冻结 Agent 公共契约` |
| **F35.3（已完成）** | Profile/Prompt 配置边界与旧 `services/agent` 清理；引入 ProfileSnapshot | 配置优先级、快照、旧引用扫描 | `feat(F35): 收敛 Agent Profile Service` |
| **F35.4（已完成）** | Inbox Msg[]、reservation、lease、claim/requeue | 并发、崩溃恢复、无丢消息测试 | `feat(F35): 建立 Inbox 原子投递` |
| **F35.5（已完成）** | `ContinueTurn`、`before-reasoning`、真实 `UserMessageEvent` 和 Assistant 边界 | 事件时序、Projection、客户端实时流测试 | `feat(F35): 完成 UserMessage 消息边界` |
| **F35.6（已完成）** | 取消、重启、架构收尾、clean install 和最终验收 | 全量门禁与 DoD | `feat(F35): 完成 Agent 与 Inbox 边界收敛` |

后续阶段的需求保留在本文作为终局约束，但在当前阶段只能作为验收边界，不能顺手实现。每个阶段开始前将本表对应行标为 `in_progress`，完成后标为 `done`，并在第 8 节记录实际变更。

### 2.0.1 F35.1 实施卡（当前唯一工作包）

**目标**：修正 Agent Service 的 Owner 方向，不改变 Agent 运行行为。

**必须修改**：

1. 新增/完善 `packages/ftre-agent/src/ftre_agent/plugin.py`，由它创建并提供唯一 `agents` Service。
2. 修改 `packages/ftre-agent-runtime/src/ftre_agent_runtime/plugin.py`：只注入 `agents` 和 Host Service，注册一个 `RuntimeFactory`，不得构造 `AgentService`，不得 `provide("agents")`。
3. 修改 `src/ftre/app/gateway/composition.py`：先加载 Agent Plugin，再加载 Runtime Plugin；保持唯一 Composition Root。
4. 增加 Owner 架构测试、Factory 注册/注销测试、Plugin 启停测试和现有 run/cancel/status 回归测试。

**本阶段明确不修改**：

- `AgentRunRequest`、Inbox `InboundMessage`、Profile 目录、UserMessageEvent、Session Projection、客户端和 `E:\ftre-agent-core`。
- ReAct、Tool、LLM、Retry、Fallback、Compaction 的行为。
- F35.2～F35.6 的任何实现；如果发现必须先改公共契约，记录为阻塞项，不在本阶段绕过。

**F35.1 验证命令**（在 `E:\ftre` 执行）：

```powershell
python -m pytest -q
python -m ruff check src tests packages
$matches = rg -n 'provide\s*=\s*\("agents"\)|AgentService\(' packages/ftre-agent-runtime src/ftre/plugins src/ftre/app 2>$null
if ($LASTEXITCODE -eq 0) { $matches; exit 1 }
if ($LASTEXITCODE -gt 1) { exit $LASTEXITCODE }
git diff --check
```

其中 `rg` 返回 1 表示“未发现违规匹配”，由脚本按成功处理；返回大于 1 才表示扫描错误。

**F35.1 验收断言**：

- 架构扫描只能找到 `ftre_agent.plugin` 的 `agents` Service Provider；Runtime Plugin 无 `AgentService(...)` 和 `provide("agents")`。
- Gateway 启动只注册一个 Runtime Factory；重复注册、未注册 Factory、关闭后 create 都返回 typed error。
- Fake Runtime 下 run/cancel/get/status/dispose 回归通过，现有 Agent 行为无变化；create/resume 契约留给 F35.2。
- 上述命令全部退出码为 0；只允许提交 F35.1 相关文件。

**F35.1 commit 门禁**：验证全部通过后才允许提交；提交前更新 TODO F35.1 为 `done`、本 PRD 对应勾选和变更记录。提交失败不得进入 F35.2。

### 2.0.2 F35.2 实施卡（当前唯一工作包）

**目标**：让 Agent Service 真正提供 `create/resume/get/list/run/stream/cancel/status/dispose`，并用 typed data model 约束 Runtime 调用；本阶段保留旧 `InboundMessage` 适配入口，供 F35.3/F35.4 迁移期间使用。

**必须修改**：

1. `ftre-agent` 新增不可变 `AgentCreateSpec`、`AgentResumeSpec`、`AgentRunRequest`、`AgentView`、`AgentEvent`、`AgentRuntimeHandle` 和 `AgentHandle` 契约。
2. `AgentService` 维护 Agent Handle 表、active Run 状态和 `AgentView`，提供 create/resume/run/stream/cancel/status/dispose。
3. `ftre-agent-runtime` 用 `AgentLoopFactory` 和 `AgentLoopHandle` 适配当前 Session-oriented `AgentLoop`；Service 不再直接持有 Loop 实例。
4. 增加 Agent API、状态跃迁、Factory Handle、typed error 和 clean import 测试。

**本阶段明确不修改**：

- Inbox 的 `InboundMessage`、队列 claim/lease/reservation、Profile 目录和 Session Projection；这些分别留给 F35.3～F35.5。
- Core ReAct、LLM、Tool、Retry、Fallback、Compaction 和客户端。

**F35.2 验证命令**（在 `E:\ftre` 执行）：

```powershell
python -m pytest -q tests/contracts tests/architecture packages/ftre-agent-runtime/tests
python -m ruff check src tests packages
git diff --check
```

**F35.2 验收断言**：

- `AgentService.create/resume` 返回 `AgentHandle`；`run/stream/cancel/status/dispose` 只通过 Handle/Factory 协议工作。
- `AgentService` 的 `get/list` 返回只读 `AgentView`，不暴露 Runtime 对象；同一 Agent 并发 Run 得到 `AgentBusyError`。
- Runtime 只注册 `AgentLoopFactory`，不把 `AgentLoop` 直接塞进 Service；Factory 缺失、重复注册、Service 关闭、Agent 不存在和非法请求都有 typed error。
- `ftre-agent` 可在没有 ftre Host 源码的解释器中导入；Core/Runtime/Host 现有回归不被破坏。
- 上述命令全部通过后才允许提交 F35.2；不得在本阶段顺手改 Inbox/Profile/Event Projection。

### 2.0.3 F35.3 实施卡（当前唯一工作包）

**目标**：让 Profile 成为独立 Host Service，按项目 > 用户 > Host 默认来源解析不可变快照；删除 `src/ftre/services/agent` 目录 Owner，不改变 Agent Runtime 的执行行为。

**必须修改**：

1. 将 `src/ftre/services/agent/profile/*`、`config.py`、`router.py` 迁移到 `src/ftre/services/agent_profile/`，更新 Composition、Session、Compaction、Team、Task、Messaging 和测试引用。
2. `AgentProfileService.resolve(ProfileQuery)` 返回 `AgentProfileSnapshot`；快照包含 LLM、prompt sources、tool policy、workspace、source trace、snapshot hash，外部不能修改内部值。
3. 保留旧 `resolve(agent_id, session_id)` 作为本阶段内部迁移调用，不能新增新的旧路径引用；F35.4 后删除该兼容入口。
4. 增加项目 Profile 优先级、用户 Profile 回退、快照 hash/不可变和旧路径扫描测试。

**本阶段明确不修改**：

- AgentService 的 Run/Msg 数据面、Inbox reservation/lease/claim、UserMessageEvent、Session Projection、客户端和 `E:\ftre-agent-core`。

**F35.3 验证命令**：

```powershell
python -m pytest -q tests/test_agent_manager.py tests/test_context_config.py tests/contracts/test_f35_profile_snapshot.py tests/architecture
python -m ruff check src tests packages
rg -n 'ftre\.services\.agent(\.| import)|services\.agent\.profile|services\.agent\.config' src packages tests
git diff --check
```

`rg` 无匹配是成功；PowerShell 严格验证使用：

```powershell
$matches = rg -n 'ftre\.services\.agent\.profile|ftre\.services\.agent\.config|services/agent/profile|services/agent/config' src packages tests -g '*.py' 2>$null
if ($LASTEXITCODE -eq 0) { $matches; exit 1 }
if ($LASTEXITCODE -gt 1) { exit $LASTEXITCODE }
```

**F35.3 commit 门禁**：上述验证全部通过，且旧 `src/ftre/services/agent` 不再存在后，才提交 `feat(F35): 收敛 Agent Profile Service`；提交后将 F35.3 标为 done、F35.4 标为 in_progress。

### 2.0.4 F35.4 实施卡（当前唯一工作包）

**目标**：把 Inbox 的持久输入转换为 Core `Msg[]`，并在 AgentService reservation 与 durable lease 双重保护下完成 claim；任何进程崩溃、Agent 失败或取消都不能静默丢失 pending 输入。

**必须修改**：

1. `QueueItem` 增加 `messages: tuple[Msg, ...]` 与 Agent profile 标识；旧 schema 仍可读取，写入升级为 schema 2。
2. `InboxRepository` 增加 inflight lease、原子 `claim_lease/ack/release`；新进程加载时丢弃旧 owner 的 inflight 项，禁止中断请求在启动阶段重复执行；未领取的 pending 仅保留为快照，等待新的 admission 或显式 `resume_pending()`。
3. `AgentService` 增加短生命周期 `RunReservation`、`try_reserve`、`release_reservation`；执行开始时消费匹配 reservation，reservation 参与 busy 判断。
4. Inbox 新版 AgentService 路径必须按 `ensure identity → reservation → history/claim lease → AgentRunRequest(Msg[]) → ack` 顺序执行；失败/取消统一 release，不得先永久 claim 再调用 Agent。
5. 扩展 Inbox 观察 Hook：`inbox/admitted`、`inbox/claimed`、`inbox/deferred`、`inbox/delivered`、`inbox/error`；Hook 失败只记录，不改变队列事实。
6. AgentService 状态变化唤醒 Inbox worker；`wait_session_quiescent` 不使用固定时间 sleep 轮询。旧 Fake Agent 的 InboundMessage 适配只作为现有回归路径，F35.6 删除。

**本阶段明确不修改**：

- `UserMessageEvent` 投影、Assistant message_id 轮换、`before-reasoning` 注入语义和客户端；这些属于 F35.5。
- `E:\ftre-agent-core`；不修改 Core Msg/Hook 类型。
- F30 LLM Retry/Fallback、F34 ToolService 和 Profile 解析行为。

**F35.4 验证命令**：

```powershell
python -m pytest -q packages/ftre-inbox/tests tests/test_inbox_service.py tests/lifecycle/test_f10_lifecycle_faults.py tests/contracts/test_f35_agent_api.py tests/architecture
python -m ruff check src tests packages
git diff --check
```

必须额外确认：

```powershell
rg -n 'claim_lease|RunReservation|AgentRunRequest|inbox/(admitted|claimed|deferred|delivered|error)' packages/ftre-inbox packages/ftre-agent src/ftre tests
```

**F35.4 commit 门禁**：上述测试、lint、diff 检查和静态搜索全部通过；lease release/recovery、reservation race、新版 Msg[] Agent 调用均有测试后，提交 `feat(F35): 建立 Inbox 原子投递`；提交后将 F35.4 标为 done、F35.5 标为 in_progress。

### 2.0.5 F35.5 实施卡（当前唯一工作包）

**目标**：在 active Turn 的下一次真实 Reasoning 前，把 Inbox next-step 作为真实 `UserMessageEvent` 写入 Session 和实时 Bus；投影先封口上一条 Assistant，再让 Core 注入同一条 `role=user` Msg 并轮换 message_id，避免 UI 依赖刷新才能看到边界。

**必须修改**：

1. `SessionEventService.emit_user_message_if_absent` 增加 `run_id`、`previous_assistant_message_id`，事件 id 仍由 `session_id + request_id` 稳定生成；metadata/data 不得放密钥或完整附件二进制。
2. `SessionEventService` 暴露当前 active Assistant 的只读 message_id 查询；Inbox 在 `before-reasoning` 交接点先取得该 id，再执行 UserMessage 持久化/广播。
3. `SessionProjection` 收到带 previous id 的 `UserMessageEvent` 时，先将对应 Assistant 设置 `finished_at/finished_reason` 并持久化；写入失败时保留 active 投影可重试，成功后才移除 active 状态。
4. Inbox `on_before_reasoning` 传入 Core `turn_id`；返回的 `BeforeReasoningResult` 使用与 Session 事件相同的用户消息 id/content，Core 负责把它加入内存并轮换下一条 Assistant message_id。
5. 覆盖普通 UserMessage、next-step、多个 next-step、重复 request、Projection 写入失败、实时 Bus 顺序和刷新恢复测试；不得修改 Core。

**本阶段明确不修改**：

- Inbox admission/lease/reservation（已在 F35.4 完成）；AgentService/F30 LLM/F34 ToolService。
- 客户端布局；只保证事件、message_id、sequence 与 Projection 数据一致。
- `ContinueTurn` 的 Core 语义；它仍是隐藏控制信号，不伪装成用户气泡。

**F35.5 验证命令**：

```powershell
python -m pytest -q tests/test_session_projection.py tests/test_session_events.py packages/ftre-inbox/tests/test_plugin_hook.py packages/ftre-inbox/tests/test_steering_delivery.py tests/test_turn_lifecycle.py tests/architecture
python -m ruff check src tests packages
git diff --check
```

必须额外确认事件顺序：`projection.update(previous assistant) → projection.upsert(user) → bus.publish(USER_MESSAGE) → Core BeforeReasoningResult`；重复 request 只产生一个 UserMsg id，投影失败不丢 active assistant。

**F35.5 commit 门禁**：上述验证全部通过，且 next-step 的实时 UserMessage、旧 Assistant immediate close、Core message_id 轮换和失败重试均有回归测试后，提交 `feat(F35): 完成 UserMessage 消息边界`；提交后将 F35.5 标为 done、F35.6 标为 in_progress。

### 2.0.6 F35.6 实施卡（当前唯一工作包）

**目标**：完成 F35 的生命周期和架构收尾，证明唯一 Owner、可逆启动/停止、lease/restart recovery、干净安装和旧符号清理均达到终局，不再引入新业务功能。

**必须修改**：

1. AgentService、Runtime Factory、Inbox、Session Event/Projection 的 `start/stop/close/dispose` 必须幂等；启动中途失败按逆序撤销 Service、Factory、Hook、Worker 和 lease 引用。
2. Runtime 停止必须取消 active Turn 并等待 completion；Inbox 停止保留未领取 pending，已领取 inflight 在下一次启动丢弃且不重投；AgentService reservation 在 stop/unregister 时全部清理。
3. 完成全局静态扫描：`ftre-agent` 不出现 Inbox/Channel/Repository/`InboundMessage`；Runtime 不反向 import Host；旧 `src/ftre/services/agent` 源码、兼容导出、重复 Owner、临时脚本和无效注释全部退出。
4. 补充 clean import、clean wheel/install、Composition reload、失败回滚、取消/重启恢复、重复 close 和跨 Session 隔离测试；不修改 `E:\ftre-agent-core` 或客户端。
5. 对照本 PRD FR/AC/DoD 逐条勾选，更新 TODO/F35、CHANGELOG 和执行记录；只有所有门禁通过才将 F35 标为 done、PRD 标为“已验收”。

**F35.6 验证命令**：

```powershell
python -m pytest -q
python -m ruff check src tests packages
git diff --check
```

静态与发行门禁：

```powershell
$matches = rg -n '^(from ftre(_inbox|\.services)|import ftre(_inbox|\.services))|class InboundMessage|class QueueItem|create_llm_handler' packages/ftre-agent/src -g '*.py' 2>$null
if ($LASTEXITCODE -eq 0) { $matches; exit 1 }
if ($LASTEXITCODE -gt 1) { exit $LASTEXITCODE }
python -m pytest -q tests/architecture tests/contracts tests/lifecycle tests/startup
```

**F35.6 commit 门禁**：全量测试、ruff、diff、clean import/wheel、生命周期恢复和静态扫描全部通过；更新所有 FR/AC 勾选和 DoD 证据后，提交 `feat(F35): 完成 Agent 与 Inbox 边界收敛`。

### 2.1 功能需求

- [x] **FR1：唯一 Agent Service Owner**（F35.1 已完成；后续阶段仍需保持该门禁）
  - `packages/ftre-agent` 提供 `AgentService` 和公共契约，并由自己的 `ftre_agent.plugin:apply` 提供 `agents` Service。
  - `ftre-agent-runtime` 不得创建、替换或再次 `provide("agents", ...)`；它只能向 `AgentService` 注册一个 `AgentFactory`。
  - 同一 Composition Root 只能存在一个 `AgentService` 和一个 Runtime Factory；重复注册必须显式报错。

- [x] **FR2：AgentService 公共 API**
  - `create(spec) -> AgentHandle`：根据 Agent 配置创建 Agent，不启动第二份 Service。
  - `resume(spec) -> AgentHandle`：从已有 Session/Run 状态恢复 Agent。
  - `get(agent_id) -> AgentView | None`、`list() -> tuple[AgentView, ...]`：读取身份与状态。
  - `run(agent_id, request) -> AgentRunResult`：提交一轮 `Msg`，返回完成/取消/失败结果。
  - `stream(agent_id, request) -> AsyncIterator[AgentEvent]`：流式产生 Assistant、Tool、UserMessage、状态和错误事件。
  - `cancel(agent_id, reason) -> CancelResult`：请求取消当前运行，幂等处理。
  - `status(agent_id) -> AgentStatus`、`dispose(agent_id) -> None`：读取状态和释放运行资源。
  - `start` 只表示内部生命周期启动（若保留），不能作为另一套消息提交入口；消息提交统一走 `run/stream`。

- [x] **FR3：Agent 配置与运行输入不携带 Inbox 模型**
  - `AgentCreateSpec` 只包含 `agent_id`、`AgentConfig`、可选的 Session/Workspace 标识和元数据。
  - `AgentRunRequest` 的输入是不可变 `tuple[Msg, ...]`，可带 `request_id`、取消令牌、运行选项；禁止出现 `InboundMessage`、`QueueItem`、Channel、Repository 类型。
  - Agent Service 不读取 `config.json`、`.ftre/agents` 或项目目录；配置由 Profile Service 解析后以快照传入。

- [x] **FR4：Runtime 只做实现和工厂注册**
  - `ftre-agent-runtime` 的 Provider Plugin 注入 `agents`、`llm`、`tools`、`system_prompt`、Session/Event Sink 和 Hook Runtime。
  - Runtime 自己拥有 AgentLoop、TurnExecutor、Core Agent 的私有创建和清理逻辑；不直接访问 ChannelManager、MCP、ToolRegistry、SessionRepository 或 `create_llm_handler`。
  - Runtime Plugin 的唯一公开动作是 `agents.register_factory(factory)` 和生命周期注销。

- [x] **FR5：Agent 状态唯一归属**
  - `AgentService` 是运行状态的权威 Owner，维护 `created/idle/running/stopping/cancelled/failed/disposed` 等状态及当前 `run_id`。
  - Inbox、Channel、客户端只能读取或投影状态，不得写入 Agent 状态；可以维护自己的队列状态。
  - 状态转移必须有合法图、时间戳和幂等规则；同一 Agent 同时最多一个 active Run。

- [x] **FR6：Agent Profile Service**
  - `AgentProfileService.resolve(query) -> AgentProfileSnapshot` 按“项目 `.ftre` > 用户 `.ftre/agents` > 全局配置”解析 Profile，并返回不可变快照。
  - 快照包含 `llm_route(provider, model, api_type, reasoning_options)`、`prompt_sources`、`tool_policy`、`workspace`、`metadata`；不包含 Tool 实例、Live Agent、队列或运行状态。
  - `SystemPromptService` 负责把 Profile 的 prompt sections 与运行上下文渲染为最终 System Prompt；Profile 不复制 Prompt 组装逻辑，避免循环依赖。
  - 将现有 `src/ftre/services/agent/profile` 迁移为语义清晰的 `src/ftre/services/agent_profile`（或独立 `ftre-agent-profile` Package）；完成迁移后删除旧 `src/ftre/services/agent` 空壳和重复 Owner。

- [x] **FR7：ToolService 与 Profile 的边界**
  - Profile 只返回工具允许/拒绝/展示策略和工具名称；ToolService 负责 `get`、`schemas`、`execute`、取消、超时、错误和生命周期。
  - Agent Runtime 只依赖 ToolService 的公开方法，不触碰具体 Registry；工具贡献方只能通过 Tool Plugin 注册。

- [x] **FR8：Inbox 是外部输入适配器和持久队列**
  - Inbox 负责 admission、持久化 pending、claim、defer、deliver、failed、discard、worker 和恢复；不负责 React 算法。
  - Channel/Session/外部调用者把输入转换成 `Msg[]`，Inbox 保存 `PendingItem`，调用 `agents.run/stream`；Agent 永远看不到 `InboundMessage`。
  - Inbox 只允许一个 Agent 的一个 active Run；busy 时项目保持 pending/deferred，不得变成不可重试的失败，也不得在 claim 后丢失。

- [x] **FR9：准入与 claim 必须可恢复**
  - `agent/before-run` 是运行策略 Hook，不是队列所有权转移点；它接收 `AgentRunRequest`，可返回 `AllowRun`、带原因的 `RejectRun` 或 `DeferRun`。
  - Inbox 在 claim 前必须完成 Agent 的原子 run reservation（或调用等价的 `try_accept`）；reservation 失败时项目仍归 Inbox。
  - Hook 拒绝、Agent 启动失败和取消都必须释放 reservation 并按策略 requeue/dead-letter；Gateway 重启不自动重投旧 inflight，未领取 pending 只保留快照等待显式唤醒，不能产生幽灵 UserMessage。

- [x] **FR10：Agent Hook 生命周期**
  - `agent/before-run`：在一次 Run 建立前执行准入策略。
  - `agent/before-reasoning`：在下一次 LLM 推理前请求待注入消息；沿用 Core 的 `BeforeReasoningResult`。
  - `agent/stop-decision`：只对自然 `COMPLETED` 的 stop 做决策；可返回 `StopTurn` 或 `ContinueTurn`。
  - `agent/run-error`：报告 Agent 级错误并允许返回明确的终止/恢复决定；LLM 的 retry/fallback 不在此 Hook 重做。
  - `agent/after-run`、`agent/status-changed`：只读观察和通知，不允许篡改运行状态。

- [x] **FR11：Inbox Hook 生命周期**
  - 决策类（waterfall）：`inbox/before-admit`、`inbox/before-claim`。
  - 事实类（parallel/observe）：`inbox/admitted`、`inbox/claimed`、`inbox/deferred`、`inbox/delivered`、`inbox/failed`、`inbox/discarded`、`inbox/changed`、`inbox/status-changed`。
  - Hook 入参必须包含 `item_id/session_id/agent_id/messages/source/status/reason` 等 typed 数据；外部 Hook 不得直接改 Repository，必须返回决定或调用 Inbox API。
  - Inbox Hook 失败策略可配置：决策 Hook 默认拒绝/不 claim，观察 Hook 默认记录并继续；每次决定都要可审计。

- [x] **FR12：ContinueTurn 与真实 UserMessage 分离**
  - `ContinueTurn` 只是 Core 的继续执行控制信号，不能作为用户消息，也不能直接交给客户端渲染。
  - 当 LLM 输出或 Tool 执行完成、且 Inbox 有 `next-step` 消息时，`agent/stop-decision` 返回 `ContinueTurn`；下一次 `before-reasoning` 才 claim 并注入正式消息。
  - `next-turn` 消息必须结束当前 Run，再由 Inbox 启动新的 Run；不得把跨 Run 消息伪装成当前 Turn 的内部 Hint。

- [x] **FR13：UserMessageEvent 是正式事实**
  - Agent Service/Runtime 在 `before-reasoning` 注入消息时必须产生一次真实 `UserMessageEvent`，其内容与 Core Memory 中写入的 `Msg` 完全一致。
  - 事件至少包含：`event_id`、`session_id`、`agent_id`、`run_id`、当前 `reply_id`、稳定 `message_id`、`source`、`content`、`previous_assistant_message_id`、`metadata`。
  - `event_id` 以 `run_id + sequence/item_id` 幂等生成；重试或重放不得重复展示/持久化同一 UserMessage。
  - 事件先于下一段 Assistant 的 `ReplyStart` 发出；客户端不需要刷新即可渲染插入的用户消息。

- [x] **FR14：Assistant 消息边界和投影顺序**
  - 在 `UserMessageEvent` 被 Session Projection 接收时，立即关闭/收起 `previous_assistant_message_id` 对应的 Assistant 消息。
  - Core 收到同一条 UserMessage 后保留在 Agent Memory，并轮换 `message_id`；下一次 LLM 输出使用新的 Assistant message id。
  - 事件顺序固定为：

    ```text
    Assistant A1 / Tool 完成
      -> agent/stop-decision: ContinueTurn
      -> agent/before-reasoning: claim U1
      -> UserMessageEvent(U1, previous=A1)
      -> Projection 关闭 A1
      -> Core Memory 写入 U1、message_id A1 -> A2
      -> ReplyStart(A2)
      -> 下一次 LLM / Tool
    ```

  - Session 持久化、Agent 内存、MessageBus 广播和前端展示必须使用同一 `message_id`、`run_id` 和顺序。

- [x] **FR15：错误、Retry、Fallback 分层**
  - LLM Provider 失败先由 F30 的 `llm/error`、Retry/Fallback 管线处理；只有最终无法恢复时才向 Runtime 暴露 Agent 级错误。
  - Agent Runtime 负责把错误映射为 `agent/run-error` 和 Run 终态；Inbox 只根据 Run 结果决定 defer/requeue/dead-letter。
  - 已输出正文或 Tool Call 后，不得在 Inbox 层无感切换模型；Fallback 的输出边界遵守 F30。

- [x] **FR16：生命周期、取消和恢复**
  - `cancel` 必须传播到 LLM、Tool、Runtime 和 Inbox reservation；取消后不触发自然 stop continuation。
  - `dispose`、插件卸载、Gateway 关闭和恢复必须幂等；未领取 pending Inbox 项目不得丢失，旧 inflight 不得在重启后再次执行，active Run 必须得到明确的 interrupted/failure 结果。
  - Agent Service、Runtime Plugin、Profile Service、Inbox 的启动顺序和关闭顺序必须由 Composition Root 明确编排。

### 2.2 非功能需求

- **一致性**：UserMessageEvent 的 event id、message id、run id 和 sequence 可重放、可去重、可审计。
- **并发**：每个 Agent 单 active Run；多个 Inbox worker 并发 claim 时必须原子互斥；不得使用固定间隔轮询代替可等待的状态/完成通知。
- **可观测性**：记录 Agent 创建/恢复、reservation、claim、defer、UserMessage 注入、Run 终态及错误原因；日志不得包含凭据和完整敏感消息。
- **兼容性**：现有 Session/WS 事件字段保持兼容，新增字段采用可选/向后兼容方式；不修改 Core 包对外协议。
- **可测试性**：所有 Package 可在无真实 API Key、无真实 Channel、无真实 Tool 的 Fake Service 下测试；架构扫描能阻止旧 Owner 和反向依赖回归。
- **安全性**：Profile 和 Tool policy 在 Agent 创建时冻结或显式版本化；Agent 不得越权读取 Workspace、MCP 或未授权工具。

## 3. 技术方案

### 3.1 目标依赖图

```text
Channel / Session / API
          |
          v
       ftre-inbox  -------- Inbox Hooks / durable pending
          |
          |  Msg[] + AgentRunRequest
          v
       ftre-agent  -------- 唯一 AgentService、状态、身份、Factory 注册点
          |
          v
   ftre-agent-runtime ----- AgentLoop / TurnExecutor / Core 适配
      |       |      |      \
      v       v      v       v
     LLM   Tools  Prompt  Event Sink
      ^       ^      ^       ^
      |       |      |       |
 F30 LLM  F34 Tool  Profile  Session/Bus Projection
 Service  Service  Service
```

### 3.2 目标目录

```text
E:/ftre/
├─ packages/
│  ├─ ftre-agent/
│  │  └─ src/ftre_agent/
│  │     ├─ contracts.py       # AgentConfig、RunRequest、Result、View
│  │     ├─ service.py         # AgentService、身份/状态、Factory 注册
│  │     ├─ events.py          # AgentEvent、UserMessageEvent 公开契约
│  │     ├─ hooks.py           # Agent Hook typed payload/result
│  │     └─ plugin.py          # 唯一提供 agents 的 Provider Plugin
│  └─ ftre-agent-runtime/
│     └─ src/ftre_agent_runtime/
│        ├─ plugin.py          # 注入依赖并 register_factory，不提供 agents
│        ├─ engine.py           # AgentLoop 实现
│        ├─ turn_executor.py    # Run/Turn 状态机
│        ├─ driver.py           # Service -> Runtime 驱动
│        └─ events.py           # Core Event -> Agent/Session Event 转换
├─ src/ftre/
│  ├─ services/
│  │  ├─ agent_profile/        # 配置解析、Profile 快照、Profile API
│  │  ├─ system_prompt/        # Prompt 组装
│  │  ├─ sessions/             # Session 事实与 Projection
│  │  ├─ messaging/             # Bus / Channel 边界
│  │  └─ tools/                # ToolService（F34）
│  ├─ plugins/builtin/
│  │  └─ ...                    # Channel、MCP、Skill 等只贡献 Service/Tool
│  └─ app/gateway/
│     └─ composition.py         # 唯一 Composition Root
└─ tests/
   ├─ architecture/
   ├─ contracts/
   ├─ lifecycle/
   ├─ integration/
   └─ inbox/
```

### 3.3 Composition 顺序

```text
1. ftre_agent.plugin:apply
   -> provide("agents", AgentService)
2. agent_profile / system_prompt / sessions / tools / llm
   -> provide Host Services
3. ftre_agent_runtime.plugin:apply
   -> inject Host Services
   -> agents.register_factory(RuntimeFactory)
4. ftre_inbox.plugin:apply
   -> inject agents、sessions、hook_runtime
   -> provide inbox
5. channels / API
   -> inject inbox，接收外部输入
```

Runtime Plugin 不得反过来创建 AgentService；Host Composition 也不得把 Runtime 的具体类直接暴露给 Channel 或 UI。

### 3.4 关键数据模型

```python
@dataclass(frozen=True)
class AgentConfig:
    llm_route: LLMRoute
    prompt: PromptSnapshot
    tool_policy: ToolPolicy
    workspace: WorkspaceRef | None
    metadata: Mapping[str, str]


@dataclass(frozen=True)
class AgentCreateSpec:
    agent_id: str
    config: AgentConfig
    session_id: str | None = None


@dataclass(frozen=True)
class AgentRunRequest:
    messages: tuple[Msg, ...]
    request_id: str
    options: RunOptions = RunOptions()


@dataclass(frozen=True)
class AgentRunResult:
    run_id: str
    status: Literal["completed", "cancelled", "failed", "interrupted"]
    usage: Usage | None
    error: AgentError | None


@dataclass(frozen=True)
class PendingItem:
    item_id: str
    session_id: str
    agent_id: str
    messages: tuple[Msg, ...]
    source: str
    status: Literal["pending", "claimed", "deferred", "delivered", "failed", "discarded"]
    request_id: str
    attempts: int


@dataclass(frozen=True)
class UserMessageEvent:
    event_id: str
    session_id: str
    agent_id: str
    run_id: str
    reply_id: str
    message_id: str
    source: str
    content: tuple[ContentBlock, ...]
    previous_assistant_message_id: str | None
    metadata: Mapping[str, str]
```

`AgentConfig`、`AgentRunRequest`、`PendingItem` 和事件必须不可变；可变队列状态只存在于 Inbox Repository，Agent Service 只保存运行时状态和索引。

### 3.5 Hook 合约

```python
# Agent hooks
agent/before-run:
    BeforeRunPayload(agent_id, request, status, reservation) \
        -> AllowRun | RejectRun(reason, retryable) | DeferRun(reason, retry_at)

agent/before-reasoning:
    BeforeReasoningPayload(run_id, message_id, memory_view) \
        -> BeforeReasoningResult(messages=tuple[Msg, ...])

agent/stop-decision:
    StopDecisionPayload(run_id, reason="completed", pending=bool) \
        -> StopTurn | ContinueTurn(prompt, reason, source)

agent/run-error:
    RunErrorPayload(run_id, error, emitted_output, tool_calls) \
        -> FailRun | RecoverRun(reason)

agent/after-run / agent/status-changed:
    payload -> None  # 观察，不改变状态

# Inbox hooks
inbox/before-admit:
    AdmitPayload -> Admit | Reject(reason)

inbox/before-claim:
    ClaimPayload(item, agent_status) -> EnterClaim(reservation) | DeferClaim(reason)

inbox/admitted / claimed / deferred / delivered / failed / discarded / changed / status-changed:
    payload -> None  # 事实通知
```

Hook 的决策类型必须带 reason、来源和 retryable/defer 信息；不得以普通 `bool` 隐式表达忙、拒绝、失败和取消。

### 3.6 UserMessage 注入算法

1. LLM 输出或 Tool 执行完成后，Runtime 让 Core 进入正常的 stop-decision 边界。
2. Inbox/Hook 判断是否存在 `next-step` 项目；存在时返回 `ContinueTurn`，仅作为 Core 继续控制信号。
3. Core 在下一次 `before-reasoning` 调度 Hook；Inbox 原子 claim 项目并返回正式 `Msg`。
4. Runtime 为该消息分配稳定 `message_id`，通过 Agent Event Sink 发出 `UserMessageEvent`，同时将同一 `Msg` 返回给 Core `BeforeReasoningResult`。
5. Session Projection 先根据 `previous_assistant_message_id` 关闭前一个 Assistant；随后 Core 把 UserMessage 写入 Memory 并轮换 Assistant `message_id`。
6. 下一次 `ReplyStart`/LLM 事件使用新的 Assistant message id。若任何步骤失败，事件幂等重放，不得重复 claim。

`next-turn` 不执行第 2～6 步，而是完成当前 Run，由 Inbox 以新的 `AgentRunRequest` 启动下一 Run。

### 3.7 DSH 参考与取舍

DSH 的可复用经验是“公共 Agent Registry/Handle”和“具体 Agent Loop 实现”分离：

| DSH 能力 | F35 对应设计 | 取舍 |
|---|---|---|
| `dsh-agent` 的 `agents` Service、注册表、`get/list/create/resume` | `ftre-agent` 的 `AgentService`、`AgentHandle`、状态和 Factory 注册 | 只借鉴公共门面和工厂方向，不引入 DSH 的 Channel/Inbox 模型 |
| `dsh-agent-loop` 的 `AgentLoop`、`create/resume`，依赖 `agents/sessions/llm/tools/systemPrompt` | `ftre-agent-runtime` Provider、RuntimeFactory、TurnExecutor | Loop 是实现方，不得再创建 `agents` Service |
| `dsh-agent-presets` | `AgentProfileService` + `SystemPromptService` | Profile 只解析并返回快照，不持有 Live Agent 或 Tool 实例 |
| `dsh-subagent` | F35 非目标，后续作为独立 Subagent Service | 不把子 Agent 编排塞进基础 AgentService |

F30 与 F34 是本阶段的上游稳定边界：

- `ftre-llm`/Provider 负责协议适配、单次调用、`llm/error`、Retry 和 Fallback；Agent 只消费稳定 LLM 接口。
- `ToolService` 负责工具定义、作用域投影、schema、执行、取消、超时和错误；Runtime 不能访问具体 Registry。
- `MessageBus`、`SessionService`、`SystemPromptService` 和 `AgentProfileService` 都是 Host Service；Agent Package 通过注入的公开协议消费它们。

这样可以避免把 DSH 的所有能力复制到一个巨大的 Agent 类中，也避免把 Inbox、Profile、Tool、LLM 再次变成 Agent 的隐式内部字段。

## 4. 接口定义

### 4.1 AgentService 示例

```python
profiles = ctx.agent_profiles
profile = await profiles.resolve(ProfileQuery(name="default", workspace=workspace))

agent = await ctx.agents.create(
    AgentCreateSpec(
        agent_id="ws_sess_32a11634f380",
        config=profile.to_agent_config(),
        session_id="ws_sess_32a11634f380",
    )
)

result = await agent.run(
    AgentRunRequest(
        messages=(Msg.user("继续处理这个文件"),),
        request_id="req-001",
    )
)

async for event in ctx.agents.stream(agent.id, request):
    await client_or_session_event_sink.publish(event)

await ctx.agents.cancel(agent.id, reason="user_cancel")
```

### 4.2 Inbox 示例

```python
item = await inbox.admit(
    session_id=session_id,
    agent_id=agent_id,
    messages=(Msg.user(text),),
    source="channel:websocket",
)

# worker 内部：claim 前先申请 Agent reservation
claimed = await inbox.claim(item.item_id)
if claimed is not None:
    result = await agents.run(
        claimed.agent_id,
        AgentRunRequest(messages=claimed.messages, request_id=claimed.request_id),
    )
    await inbox.complete(claimed.item_id, result)
```

`inbox.claim`、Agent reservation 和失败回滚必须由同一个可恢复流程协调；示例不代表允许“先删除队列项、再尝试运行”。

### 4.3 Profile 示例

```python
snapshot = await agent_profiles.resolve(
    ProfileQuery(
        name="default",
        workspace=workspace,
        user_root=user_root,
        project_root=project_root,
    )
)
# snapshot 只描述路由、提示词来源、工具策略和工作区，不含 Live Agent 或 Tool 实例
```

## 5. 验收标准

- [x] **AC1：唯一 Owner**：架构扫描确认只有 `ftre_agent.plugin` 提供 `agents`；`ftre-agent-runtime` 不出现 `provide("agents")`、AgentService 构造或第二个 Registry。
- [x] **AC2：独立契约**：单独安装/import `ftre-agent` 不加载 Runtime、Host、Channel、Inbox、Session Repository 或 Core 的具体实现。
- [x] **AC3：工厂注册**：Runtime Plugin 能注册/注销 Factory；重复注册、缺失 Factory、关闭后创建均得到 typed error；普通 create/resume 回归通过。
- [x] **AC4：API 边界**：`AgentService`/`AgentHandle` 的公开签名只出现 `AgentConfig`、`AgentRunRequest`、`Msg`、Agent Event 和状态，不出现 `InboundMessage`、`QueueItem`、Channel 或 Repository。
- [x] **AC5：Profile 隔离**：按项目 > 用户 > 全局的优先级返回不可变 Profile；修改源文件不会改变已创建 Agent 的快照；Profile 不创建 Agent、不持有状态、不实例化 Tool。
- [x] **AC6：busy 无丢失**：并发提交两个 Inbox 项目时只接受一个 active Run；另一个保持 pending/deferred，Agent Hook 拒绝或 Runtime 启动失败后项目可重试且无幽灵消息。
- [x] **AC7：真实 UserMessage**：在下一 React 边界注入 `next-step` 时，客户端事件流收到一次 `UserMessageEvent`；事件刷新前可见，刷新后由 Session 投影恢复；同一 event id 重放不重复。
- [x] **AC8：消息边界**：UserMessageEvent 到达时前一个 Assistant 立即关闭；Core Memory 含同一 Msg；下一段 Assistant 使用新 `message_id`；事件顺序符合 FR14。
- [x] **AC9：ContinueTurn 语义**：`ContinueTurn` 不直接作为 UI UserMessage；自然完成才执行 stop-decision；错误、取消、最大轮数不被错误续跑。
- [x] **AC10：next-turn 隔离**：跨 Run 的 Inbox 消息结束当前 Run 后由新 Run 处理，不产生同一 Run 内的伪造 Reply 边界。
- [x] **AC11：Hook 合约**：Agent/InBox 决策 Hook 使用 typed decision 和 reason；观察 Hook 失败不会破坏主流程；Hook 顺序、failure policy 和审计日志有测试覆盖。
- [x] **AC12：分层错误**：LLM Retry/Fallback 仍由 F30 管线处理；最终错误才到 `agent/run-error`；Inbox 只处理队列结果，不重复调用模型切换。
- [x] **AC13：生命周期**：cancel、dispose、Gateway shutdown、Plugin reload、重启恢复均幂等；LLM/Tool/Inbox reservation 释放，pending 项目无丢失。
- [x] **AC14：旧 Owner 清理**：Profile/Router 迁移后删除旧 `src/ftre/services/agent` Owner、桥接入口、兼容导出和无引用空目录；全局搜索无陈旧引用。
- [x] **AC15：工程验收**：`python -m pytest -q`、`python -m ruff check src tests packages`、架构扫描和 `git diff --check` 全部通过；测试不依赖真实 API Key。

## 6. 测试计划

### 6.1 单元测试

- AgentService 状态机、Factory 注册、create/resume/get/list/status、重复运行和幂等 dispose。
- AgentConfig/Profile 优先级、快照不可变性、Prompt 组装和 Tool policy 隔离。
- AgentRunRequest/Result、Hook decision、UserMessageEvent 序列化和 event id 计算。
- Inbox admission、原子 claim/reservation、defer/requeue、失败回滚、重启恢复和 dead-letter。

### 6.2 集成测试

- Fake LLM + Fake Tool 下跑完整 React：Assistant → Tool → ContinueTurn → UserMessageEvent → 新 Assistant。
- 验证 Core Memory、AgentEvent Sink、Session Projection、MessageBus 和客户端订阅看到相同 message id/顺序。
- 并发 Inbox worker 与 Agent cancel、LLM error、Tool error、Runtime crash 组合场景。
- F30 Retry/Fallback 最终失败只产生一次 Agent Run Error，Inbox 可按策略重试，不重复切模型。

### 6.3 架构与发行测试

- 扫描 `ftre_agent` 不得 import Host/Inbox/Channel/Repository；Runtime 不得提供 `agents`。
- 扫描 `src/ftre/services/agent` 旧路径、旧 AgentService、`InboundMessage` 在 Agent Package 中的引用必须为零。
- clean install 后只加载一次 Agent Service、一次 Runtime Factory；插件卸载和重新加载不泄漏 worker。

### 6.4 手动验证

1. 启动 Gateway，确认 Composition 日志按 FR3.3 顺序加载。
2. 连续发送两条消息，确认第二条在第一条运行时保持 Inbox pending，不出现失败幽灵消息。
3. 在 Tool 完成后注入一条 `next-step`，确认无需刷新客户端即可看到 UserMessage，旧 Assistant 收起、新 Assistant 开始。
4. 刷新 Session，确认 UserMessage、Assistant 边界和状态与实时流一致。
5. 取消、重启、恢复后检查 Inbox pending 项、Agent status、Session event 和日志。

## 7. 迁移计划

| 阶段 | 工作 | 结果 |
|---|---|---|
| F35.1 | 从 `ftre-agent-runtime.plugin` 移出 AgentService，新增 `ftre-agent.plugin`，固定 Composition 顺序 | 唯一 `agents` Owner |
| F35.2 | 冻结 AgentConfig/RunRequest/Result/Handle/Event/Hook 契约，Runtime 改为 Factory Provider | Agent Package 可独立测试 |
| F35.3 | 将 Profile/config/router 迁移到 `agent_profile` Host Service，删除旧 Agent 目录与重复导出 | Profile 与 Agent 运行时分离 |
| F35.4 | Inbox 改为 Msg[] + durable PendingItem，加入 reservation、before-claim 和恢复流程 | busy 不丢消息 |
| F35.5 | 接通 stop-decision、before-reasoning、UserMessageEvent、Projection immediate close 和 message id 轮换 | 前端实时得到真实用户消息 |
| F35.6 | 补齐 cancel/shutdown/restart、架构扫描、clean install、全量回归并删除尾巴 | F35 可验收 |

每个子阶段完成后先更新本 PRD 的变更记录和验收勾选，再进入下一子阶段；不得以兼容壳代替迁移。

## 8. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-27 | 初始草稿：汇总 Agent Service、Runtime、Profile、Inbox、Hook、UserMessage 边界和最终事件顺序 | 固化多轮架构讨论，作为 F35 唯一实施依据 |
| 2026-08-27 | 扩展为实施规格：补充术语/Owner 矩阵、完整 API 与错误、三套状态机、Inbox 原子 claim、事件持久化/恢复、Hook 失败矩阵、Profile 配置、当前代码迁移映射、架构门禁、观测、安全、版本和 DoD | 初稿不足以指导这一规模的边界重构，需要把讨论结论转成可执行约束 |
| 2026-08-27 | F35.1 实施完成：`ftre-agent` 独立提供 `agents`，Runtime 改为 Factory Provider；全量 `675 passed`、ruff、架构扫描和 diff 检查通过 | 完成第一阶段 Owner 分离，后续阶段暂不启动 |
| 2026-08-27 | F35.1 收尾：新增 `AgentRuntimeFactory` Protocol、`AgentLoopFactory` 包装器、AgentService typed errors，并修正 `rg` 无匹配时的验收脚本 | 消除“Loop 冒充 Factory”、通用异常和验证命令退出码偏差 |
| 2026-08-27 | F35.2 实施完成：冻结 AgentCreate/Resume/RunRequest、Handle、View、Event、状态和 typed errors；补充 Fake Runtime 契约测试；全量 `675 passed` | 将 AgentService 数据面从隐式 Loop 调用收敛为可测试的公开契约 |
| 2026-08-27 | F35.3 实施完成：Profile/config/router 迁移至 `src/ftre/services/agent_profile`；项目 > 用户 > Host 优先级解析为不可变 ProfileSnapshot；迁移所有生产/测试引用；全量 `682 passed`、ruff、diff 检查通过 | 删除旧 `src/ftre/services/agent` Owner，隔离 Profile 与 Agent Runtime |
| 2026-08-27 | F35.4 实施完成：Inbox `Msg[]`、AgentService RunReservation、durable lease `claim_lease/ack/release` 与 orphan recovery；新增 Inbox admitted/claimed/deferred/delivered/error Hook；全量 `693 passed`、专项 `217 passed`、ruff、diff 检查通过 | 在 Agent 调用前建立原子准入与可恢复投递，避免 busy/崩溃/取消丢失输入 |
| 2026-08-27 | F35.5 实施完成：UserMessageEvent 携带 run/previous assistant 坐标；Projection 在用户消息前即时封口 Assistant，失败保留 active 可重试；Inbox before-reasoning 传递 turn_id；全量 `695 passed`、专项 `197 passed`、ruff、diff 检查通过 | 让 next-step 在实时流中形成真实用户消息和 Assistant 边界，刷新前后状态一致 |
| 2026-08-27 | F35.6 实施完成：RuntimeInput 收口 Agent/Runtime 输入，删除 Agent InboundMessage 兼容路径；补齐 before-admit/failed/discarded Hook、失败结果 lease release、取消/重启恢复、clean wheel/import 与终局架构扫描；全量 `703 passed`、专项 `272 passed`、ruff、diff 检查通过 | 完成 F35 生命周期、恢复和旧 Owner/符号收尾，所有阶段按独立 commit 闭环交付 |
| 2026-08-28 | F36 立项：在保留 F35 AgentService/Inbox/UserMessage 边界的基础上，继续迁移 Core ReAct、公共 Msg/Event/Hook 和 Runtime Tool 边界；F36.5 将修正真实 stream 与状态投影的最后耦合 | F35 固定了入口和消息边界，F36 负责移除独立 Core 发行边界，不回滚 F35 已验收语义 |

## 9. 术语、角色与责任矩阵

### 9.1 术语

| 术语 | 定义 | 不代表什么 |
|---|---|---|
| Agent | 一个可恢复的运行身份，绑定配置快照、Session 和 Runtime 状态 | 不是 Channel、不是 Inbox 项目、不是一个 LLM 请求 |
| AgentService | `ftre-agent` 对外的稳定 Facade，负责 Agent 身份、状态、Run 入口和 Runtime Factory 调度 | 不实现 ReAct，不解析配置，不执行具体 Tool |
| AgentHandle | AgentService 返回给调用者的窄操作句柄 | 不拥有第二份状态，不绕过 AgentService 调用 Runtime |
| Agent Runtime | 具体 ReAct/Turn 执行实现，由 `ftre-agent-runtime` 提供 | 不是公共 Service Owner |
| Run | 一次从输入消息开始到 completed/cancelled/failed 的完整 Agent 执行 | 不等同于一次 LLM attempt |
| Turn | Run 内的一轮 reasoning/acting 循环 | 不等同于用户消息；一个 Run 可以有多个 Assistant message |
| Inbox Item | Inbox 持久化的外部待处理消息项目 | 不传入 Agent API；必须先转换成 Msg |
| next-step | 当前 Run 在下一个 `before-reasoning` 边界注入的正式用户消息 | 不是 `ContinueTurn.prompt` 的隐藏 Hint |
| next-turn | 当前 Run 完成后再启动新 Run 的消息 | 不能伪装成当前 Run 的内部消息 |
| reservation | AgentService 给 Inbox 的一次 active Run 占用凭证 | 不是 claim 完成；失败必须释放 |
| UserMessageEvent | 真实用户消息事实事件，供 Session、Bus、客户端和审计消费 | 不是内部 Hook 返回值，不是 `ContinueTurn` |

### 9.2 责任矩阵

| 能力 | 唯一 Owner | 允许依赖 | 明确禁止 |
|---|---|---|---|
| Agent 身份、状态、Run 入口 | `ftre-agent.AgentService` | RuntimeFactory、公开 Service 协议 | Channel、Inbox Repository、ToolRegistry、配置文件 |
| ReAct、Turn、Core Agent 适配 | `ftre-agent-runtime` | AgentService、LLM、ToolService、Prompt、Event Sink、Hook Runtime | 再提供 `agents`、直接创建 Host Service |
| LLM 调用、协议适配、Retry、Fallback | F30 `ftre-llm` 及 Provider | Provider 配置、请求/流协议 | Agent Runtime 自己 retry/fallback |
| 工具 schema 与执行 | F34 `ToolService` | Tool Plugin、权限、取消、超时 | Agent/Profile 访问具体 Registry |
| Profile 解析 | `AgentProfileService` | Config 文件、SystemPrompt、Tool policy | 创建 Live Agent、维护 Agent status |
| System Prompt 组装 | `SystemPromptService` | Profile snapshot、运行上下文 | 读取 Inbox、执行 Tool |
| admission/queue/claim/recovery | `ftre-inbox` | AgentService、Session、Repository、Hook Runtime | 修改 Core ReAct、直接写 Agent status |
| Session 事实与 Projection | `SessionService`/Projection | Agent Event、UserMessageEvent | 依赖 Inbox 内部状态 |
| Channel/WebSocket | Channel Plugin | Inbox、Session、MessageBus | 直接创建 Runtime 或 Core Agent |

任何新增代码都必须能在该矩阵中找到唯一 Owner；无法归类的能力先停留在 PRD 评审，不得随意放进 AgentService。

## 10. AgentService 详细契约

### 10.1 服务生命周期

```python
class AgentService(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...

    def register_factory(self, factory: AgentFactory) -> Registration: ...
    def unregister_factory(self, registration: Registration) -> None: ...

    async def create(self, spec: AgentCreateSpec) -> AgentHandle: ...
    async def resume(self, spec: AgentResumeSpec) -> AgentHandle: ...
    def get(self, agent_id: str) -> AgentView | None: ...
    def list(self) -> tuple[AgentView, ...]: ...

    async def run(self, agent_id: str, request: AgentRunRequest) -> AgentRunResult: ...
    async def stream(
        self, agent_id: str, request: AgentRunRequest
    ) -> AsyncIterator[AgentEvent]: ...
    async def cancel(self, agent_id: str, reason: str) -> CancelResult: ...
    def status(self, agent_id: str) -> AgentStatus: ...
    async def dispose(self, agent_id: str) -> None: ...
```

要求：

1. `register_factory` 只能成功一次；第二个不同 Factory、重复 token、Runtime 版本不兼容都返回 typed error。
2. `create` 只允许在 Service 已启动且 Factory 已注册后执行；Agent id 冲突必须拒绝，禁止静默覆盖。
3. `resume` 必须校验 Session/Run checkpoint 与 `AgentConfig.snapshot_hash`；不一致时返回 `ResumeConflictError`，不隐式使用新配置。
4. `run` 和 `stream` 使用相同的 Run 建立逻辑；`run` 只是消费完整 stream 的便捷方法，不得有第二条状态机。
5. `cancel` 必须幂等：已完成/已取消返回已知结果；未知 Agent 返回 `AgentNotFoundError`。
6. `close` 后所有新操作返回 `ServiceClosedError`；已有 Run 得到 `interrupted` 终态，Reservation 和 worker 全部释放。

### 10.2 AgentHandle

```python
class AgentHandle(Protocol):
    @property
    def id(self) -> str: ...

    def view(self) -> AgentView: ...
    def status(self) -> AgentStatus: ...
    async def run(self, request: AgentRunRequest) -> AgentRunResult: ...
    async def stream(self, request: AgentRunRequest) -> AsyncIterator[AgentEvent]: ...
    async def cancel(self, reason: str) -> CancelResult: ...
    async def dispose(self) -> None: ...
```

Handle 的所有方法必须委托回 AgentService，以便状态、并发锁、审计和取消只有一份实现；Handle 不暴露 Runtime、Core Agent、Memory 或 Tool Registry。

### 10.3 RuntimeFactory

```python
class AgentFactory(Protocol):
    name: str
    version: str

    async def create(
        self,
        spec: AgentCreateSpec,
        services: RuntimeServices,
        emit: AgentEventSink,
    ) -> RuntimeHandle: ...

    async def resume(
        self,
        spec: AgentResumeSpec,
        services: RuntimeServices,
        emit: AgentEventSink,
    ) -> RuntimeHandle: ...
```

`RuntimeServices` 是显式依赖结构，只允许包含 `llm`、`tools`、`system_prompt`、`sessions`、`message_bus`、`hook_runtime` 和取消/时钟等公开协议。Factory 不得接收 `ctx` 后自行查找任意 Service，也不得把 Host 容器传入 Core。

### 10.4 错误类型

| 错误 | 触发条件 | 调用者动作 | 是否产生 UserMessageEvent |
|---|---|---|---|
| `ServiceClosedError` | Service 已关闭 | 等待下一次启动或报告系统不可用 | 否 |
| `FactoryNotRegisteredError` | Runtime 尚未注册 | 阻止创建，报警 | 否 |
| `AgentAlreadyExistsError` | create id 冲突 | 使用 get/resume 或更换 id | 否 |
| `AgentBusyError` | active Run 已存在 | Inbox defer；直接 API 返回 busy | 否 |
| `RunConflictError` | request id 重复但 payload 不同 | 拒绝并报警 | 否 |
| `InvalidMessageError` | Msg 不完整/含禁止对象 | 调用者修复输入 | 否 |
| `CancellationRequested` | 用户或系统取消 | 完成 cancel 清理 | 否，除非此前已正式注入 |
| `AgentExecutionError` | Runtime 最终失败 | 生成 Agent error/Run failed | 否，除非此前已正式注入 |

错误必须包含 `code`、`message`、`retryable`、`run_id`、`agent_id` 和安全的 `details`；不得把原始凭据、完整 prompt 或完整工具参数写入错误文本。

## 11. 三套状态机

### 11.1 Agent 状态

```text
CREATED -> IDLE -> RUNNING -> IDLE
                    |  |
                    |  +-> STOPPING -> IDLE
                    |                 \-> CANCELLED
                    +-----> FAILED
IDLE/CANCELLED/FAILED -> DISPOSED
```

| 当前状态 | 事件 | 下一状态 | 约束 |
|---|---|---|---|
| CREATED | runtime_ready | IDLE | 必须有 Factory/runtime handle |
| IDLE | run_accepted | RUNNING | 建立唯一 reservation/run_id |
| RUNNING | natural_completed | IDLE | 只有此路径可执行 stop-decision |
| RUNNING | cancel_requested | STOPPING | 传播取消，禁止 continuation |
| STOPPING | runtime_cancelled | CANCELLED | 释放 LLM/Tool/Reservation |
| RUNNING | terminal_error | FAILED | 等待 F30 最终错误后进入 |
| IDLE/CANCELLED/FAILED | dispose | DISPOSED | 幂等释放资源 |

禁止的跃迁必须报错而不是自动修正，例如 `IDLE -> IDLE` 重复 run、`DISPOSED -> RUNNING`、`FAILED -> RUNNING`（必须先显式 resume）。

### 11.2 Run 状态

```text
ACCEPTED -> RUNNING -> COMPLETED
                 |  |-> CANCELLED
                 |  |-> FAILED
                 \----> INTERRUPTED
```

Run 记录至少有：`run_id`、`request_id`、`agent_id`、`session_id`、`reply_id`、`started_at`、`finished_at`、`terminal_reason`、`attempt_count`、`last_event_sequence`、`config_snapshot_hash`。

### 11.3 Inbox Item 状态

```text
ADMITTED -> PENDING -> CLAIMED -> DELIVERED
              |          |          |
              v          v          v
           DISCARDED  DEFERRED   FAILED -> PENDING/DEAD_LETTER
```

状态更新必须带 `version` 和 `updated_at`，使用 compare-and-swap 或等价事务；worker 失联后由 lease timeout 把 `CLAIMED` 恢复为 `PENDING`，不能永久卡住。

## 12. Inbox 详细协议与并发规则

### 12.1 Inbox API

```python
class InboxService(Protocol):
    async def admit(self, request: AdmitRequest) -> InboxReceipt: ...
    async def get(self, item_id: str) -> PendingItem | None: ...
    async def list(self, agent_id: str, *, status: ItemStatus | None = None) -> tuple[PendingItem, ...]: ...
    async def claim(self, item_id: str) -> ClaimResult: ...
    async def defer(self, item_id: str, reason: str, *, retry_at: datetime | None = None) -> None: ...
    async def complete(self, item_id: str, result: AgentRunResult) -> None: ...
    async def fail(self, item_id: str, error: AgentError, *, retryable: bool) -> None: ...
    async def discard(self, item_id: str, reason: str) -> None: ...
    async def recover_expired_leases(self) -> int: ...
```

`admit` 只接受已经转换好的 `Msg[]`。Channel 的原始文本、附件、权限上下文在进入 Inbox 前完成归一化；Inbox 保存足以重放的一份不可变输入和来源元数据。

### 12.2 原子 claim 流程

```text
读 PENDING item
  -> inbox/before-claim
  -> AgentService.try_reserve(agent_id, item.request_id)
       ├─ busy: 保持 PENDING/DEFERRED，发 inbox/deferred
       └─ success: 得到 reservation token
  -> CAS PENDING -> CLAIMED（写入 lease）
       ├─ CAS 失败: 释放 reservation，重新读取 item
       └─ 成功: 调用 AgentService.run(reservation, request)
```

如果 `CAS` 成功后 Runtime 启动失败，必须执行 `release reservation + fail/requeue` 的补偿；只有 UserMessageEvent 已成功提交且对应 Core Memory 已确认后，项目才允许进入 `DELIVERED`。

### 12.3 next-step 与 next-turn

| 类型 | 当前 Run | 进入 Core 的方式 | Inbox 完成时机 | 前端效果 |
|---|---|---|---|---|
| `next-step` | 不结束 | stop-decision → before-reasoning → 正式 Msg | 当前 Run 完成后标记 delivered | 立即看到 UserMessage，旧 Assistant 关闭，新 Assistant 开始 |
| `next-turn` | 结束 | 当前 Run 正常 completed；新 Run 的首个 request | 新 Run accepted 后处理 | 两个独立 Run/reply 边界 |

决策不能只依赖字符串；`PendingItem.delivery_mode` 必须是枚举并持久化。

## 13. UserMessageEvent 协议、持久化和重放

### 13.1 事件信封

```json
{
  "event_type": "user_message",
  "event_id": "umev:run_123:step_4:item_456",
  "session_id": "ws_sess_32a11634f380",
  "agent_id": "ws_sess_32a11634f380",
  "run_id": "run_123",
  "reply_id": "reply_123",
  "message_id": "msg_user_456",
  "sequence": 18,
  "source": "inbox:channel:websocket",
  "previous_assistant_message_id": "msg_assistant_a1",
  "content": [{"type": "text", "text": "继续处理这个文件"}],
  "metadata": {"inbox_item_id": "item_456"}
}
```

字段规则：

- `event_id` 全局唯一且可确定性计算；同一 `run_id + item_id` 只能产生一个正式 UserMessageEvent。
- `sequence` 在同一 Session/Run 单调递增；事件乱序必须被 Session/Bus 拒绝或暂存，而不是静默覆盖。
- `message_id` 是消息事实的主键；同一 Msg 在 Core Memory、Session Projection、Bus、客户端中不可使用不同 id。
- `previous_assistant_message_id` 为空表示没有可关闭的 Assistant；不允许依赖“当前最后一条消息”猜测。
- `content` 只允许 Core 支持的 ContentBlock；原始 Inbox 元信息放入 `metadata`，不混进可见文本。

### 13.2 双写一致性

注入流程使用“事件幂等记录 + Core Memory 写入确认 + Projection”三步：

1. 预写 `UserMessageEvent` 的幂等键和 `INJECTING` 状态。
2. 将同一 `Msg` 交给 Core `BeforeReasoningResult`，Core 确认写入 Memory 并轮换 message id。
3. 将事件状态更新为 `COMMITTED`，Session Projection 关闭旧 Assistant 并广播。

如果进程在任一步骤崩溃，重启扫描 `INJECTING`：

- Memory 已有相同 `message_id`：补发/补投影，不重复插入；
- Memory 没有：重新执行一次同一幂等注入；
- 无法判断：标记 `RECOVERY_REQUIRED`，阻止该 Agent 继续消费，等待恢复任务处理。

### 13.3 Session Projection 规则

收到 `UserMessageEvent` 时必须在同一投影事务中：

1. 校验 `event_id`、`sequence`、`message_id` 和 `previous_assistant_message_id`。
2. 将前一个 Assistant 的 `finished_at` 设置为当前事件时间，并标记为 closed。
3. 插入 UserMsg（幂等）；重复事件只返回已有记录。
4. 广播 UserMessageEvent/Session 更新；禁止等待下一次 Assistant 事件才关闭旧消息。

这样可以解决“消息已经继续运行但前端旧 AI 大对象仍然展开、刷新后才修正”的问题。

## 14. Hook 调度、作用域与失败矩阵

### 14.1 调度规则

| Hook | 调度次数 | 类型 | 可否改变主流程 | 超时默认 |
|---|---:|---|---|---:|
| `agent/before-run` | 每个 Run attempt 一次 | waterfall | 可以 allow/reject/defer | 拒绝 |
| `agent/before-reasoning` | 每个 reasoning boundary 一次 | waterfall | 可以提供 Msg | 返回空结果 |
| `agent/stop-decision` | 仅自然 completed 一次 | waterfall | stop/continue | StopTurn |
| `agent/run-error` | 每个最终 Agent 错误一次 | waterfall | recover/fail | FailRun |
| `agent/after-run` | 每个 Run 一次 | parallel/observe | 不可以 | 记录并继续 |
| `agent/status-changed` | 每次合法状态跃迁一次 | parallel/observe | 不可以 | 记录并继续 |
| `inbox/before-admit` | 每个 admit 一次 | waterfall | admit/reject | Reject |
| `inbox/before-claim` | 每次 claim 尝试一次 | waterfall | claim/defer | Defer |
| `inbox/*` 事实 Hook | 每个事实一次 | parallel/observe | 不可以 | 记录并继续 |

同一个 Hook 的重试不会生成新的业务事实 id；决策 Hook 可以重试执行，但必须携带同一 `operation_id` 并记录 attempt。

### 14.2 失败矩阵

| 失败点 | Agent 状态 | Inbox 状态 | 是否发 UserMessageEvent | 后续动作 |
|---|---|---|---|---|
| before-run reject busy | 保持 IDLE/RUNNING | PENDING/DEFERRED | 否 | 按 retry_at 再 claim |
| before-run reject policy | 不启动 Run | DISCARDED 或人工处理 | 否 | 记录 policy reason |
| before-reasoning Hook 超时 | RUNNING 或 FAILED（按策略） | CLAIMED | 否 | 不确认 delivered；重试/恢复 |
| Event 预写失败 | RUNNING | CLAIMED | 否 | 重试预写，禁止继续 LLM |
| Event 已提交但 Core 写入失败 | FAILED/RECOVERY_REQUIRED | CLAIMED | 已有预写记录 | 进入恢复扫描，不重复事件 |
| LLM 可重试错误 | RUNNING | CLAIMED | 否 | 交给 F30 Retry |
| LLM 最终错误 | FAILED | FAILED/REQUEUE | 否（若此前无注入） | 交给 Inbox failure policy |
| Tool 错误 | RUNNING/FAILED | CLAIMED | 否 | 由 Agent/Tool policy 决定 |
| 用户 cancel | CANCELLED | PENDING 或 FAILED | 否 | 释放 reservation，不 stop continuation |
| 客户端断开 | Agent 继续或 cancel，按调用策略 | 不改变事实状态 | 不影响已提交事件 | 由 Session 恢复订阅 |

### 14.3 Stop Hook 特别规则

Core 当前只在自然 `COMPLETED` 路径调用 `agent/stop-decision`；错误、取消、最大轮数不能通过 Stop Hook 伪造 continuation。Inbox 不得绕过这一约束直接调用 `ContinueTurn`。

## 15. Agent Profile 与配置解析规格

### 15.1 来源与优先级

按字段合并但按来源覆盖，优先级固定为：

```text
项目：<project>/.ftre/agent* 与项目级 profile
  > 用户：C:/Users/<user>/.ftre/agents/<name>.*
  > 全局：C:/Users/<user>/.ftre/config.json 的 agents/default
  > 代码默认值（仅安全默认，不含凭据）
```

解析器必须报告每个字段最终来源、文件版本/hash 和冲突；JSON/YAML 语法错误返回结构化 `ProfileConfigError`，禁止静默回退到另一个模型造成“看似能运行、实际路由错”的问题。

### 15.2 ProfileSnapshot

```python
@dataclass(frozen=True)
class AgentProfileSnapshot:
    name: str
    snapshot_hash: str
    llm_route: LLMRouteRef
    prompt_sources: tuple[PromptSource, ...]
    tool_policy: ToolPolicy
    workspace: WorkspaceRef | None
    env_refs: tuple[str, ...]
    source_trace: tuple[ConfigSource, ...]
    created_at: datetime
```

`env_refs` 只能引用环境变量/Secret 名称，不能把实际 API key 放入快照、日志、Session 或 UserMessageEvent。创建 Agent 后 ProfileSnapshot 不随文件热更新；重新加载必须显式 `resume`/`recreate`。

### 15.3 Profile 与 SystemPrompt 分工

```text
ProfileService.resolve(query)
  -> ProfileSnapshot（路由、提示词来源、工具策略）
SystemPromptService.compose(snapshot, runtime_context)
  -> RenderedSystemPrompt
AgentService.create(config)
  -> 保存 snapshot_hash + rendered prompt 版本
```

Profile 不返回已经混入用户 Msg 的最终 messages；调用者决定首轮 Msg，Runtime 决定每轮 Memory 如何拼接。

## 16. 当前代码迁移映射

| 当前位置/问题 | 目标动作 | 完成判据 |
|---|---|---|
| `packages/ftre-agent-runtime/src/ftre_agent_runtime/plugin.py` 创建 `AgentService` 并 `provide("agents")` | 删除 Service 构造和 provide；改为注入 `agents` 并 register Factory | 文件只保留 Runtime Plugin 生命周期 |
| `packages/ftre-agent/src/ftre_agent/service.py` Facade 契约不完整 | 补齐 Service、Handle、Factory、状态、错误和 Event API | clean import 可独立运行 Fake Factory |
| `packages/ftre-agent/src/ftre_agent/hooks.py` 的 `BeforeRunPayload` 携带 InboundMessage | 改为 `AgentRunRequest`/reservation 语义 | Agent Package 搜索不到 InboundMessage |
| `src/ftre/app/gateway/composition.py` 仅加载 Runtime Plugin | 显式加载 Agent Plugin，再加载 Runtime Provider | 启动日志可见唯一 Owner |
| `src/ftre/services/agent/profile/` | 迁移为 `src/ftre/services/agent_profile/`，更新路由/导入/插件 | 旧目录删除且无 import |
| `src/ftre/services/agent/` 旧 `__init__`/桥接 | 删除空壳和兼容导出 | 全局扫描无旧 Agent Owner |
| `packages/ftre-inbox/src/ftre_inbox/service.py` `InboundMessage`、busy polling | 输入改成 `Msg[]`、reservation、lease/事件通知 | 无 20ms 固定轮询；busy 不丢项 |
| `packages/ftre-inbox/src/ftre_inbox/plugin.py` 直接返回 raw mappings | 返回 typed BeforeReasoningResult，并通过 Agent Event Sink 产生事件 | Msg/Event 内容一致 |
| `packages/ftre-inbox/src/ftre_inbox/hooks.py` 现有 before-claim/changed | 保留并扩展 admit/claim/delivery/error/status hooks | 决策/观察语义明确 |
| `src/ftre/services/session/events.py` 用户事件 reply id 与当前 Run 脱节 | 增加 run/reply/message/previous assistant 字段和幂等键 | event 可重放且不重复 |
| `src/ftre/services/session/projection.py` UserMessage 不立即关闭 Assistant | 在 UserMessage Projection 分支完成 immediate close | 无需刷新即可出现消息边界 |
| `E:/ftre-agent-core/src/ftre_agent_core/` | 只读依赖，禁止本阶段修改 | core git diff 为零 |

迁移期间允许在本地分支短暂使用 codemod 或脚本，但不得把兼容适配层、旧导出、废弃注释和双 Owner 带入最终提交。

## 17. 依赖方向与架构门禁

### 17.1 允许的 import 方向

```text
ftre-agent contracts/service
    <- ftre-agent-runtime plugin
    <- ftre-inbox
    <- Host API/Channel adapters

ftre-agent-runtime
    -> ftre-agent public contracts
    -> ftre-llm / ToolService / Prompt / Session / Hook protocols

AgentProfileService -> config sources + SystemPrompt/Tool policy types
Inbox -> AgentService + Session/Repository + Inbox Hook Runtime
```

### 17.2 禁止项扫描

提交前必须执行等价扫描并将结果保存到审查记录：

```text
ftre-agent 中禁止：
  InboundMessage | QueueItem | Channel | Repository | ToolRegistry
  Host composition import | concrete MCP/Workspace | create_llm_handler

ftre-agent-runtime 中禁止：
  provide("agents") | AgentService(...) | second registry
  direct ChannelManager/MCP/ToolRegistry/SessionRepository

src/ftre/services/agent 中禁止：
  live Agent owner | AgentLoop | Runtime factory | compatibility re-export
```

架构测试失败即阻止合并，不得以“当前测试能过”豁免。

## 18. 可观测性与审计规格

### 18.1 结构化日志

所有日志使用 `logging`，至少带以下字段：

| 事件 | 必填字段 |
|---|---|
| agent_created/resumed | `agent_id`, `session_id`, `profile_hash`, `runtime_factory`, `status` |
| run_accepted/rejected | `agent_id`, `run_id`, `request_id`, `decision`, `reason`, `reservation_id` |
| inbox_claim/defer | `item_id`, `agent_id`, `status_from`, `status_to`, `attempt`, `retry_at` |
| user_message_injected | `event_id`, `message_id`, `run_id`, `sequence`, `previous_assistant_message_id`, `source` |
| run_finished | `agent_id`, `run_id`, `status`, `elapsed_ms`, `usage`, `error_code` |
| recovery_scan | `item_id/event_id`, `recovery_action`, `result` |

日志只记录 token 数量、hash、长度和截断摘要；禁止记录 API key、完整 System Prompt、完整工具参数和完整用户附件。

### 18.2 指标

- `agent_active_runs`：按 Agent/Runtime 统计，必须不大于 Agent 数。
- `agent_run_total{status}`、`agent_run_duration_seconds`。
- `inbox_pending_items`、`inbox_claim_conflicts_total`、`inbox_requeue_total`。
- `user_message_injection_total{result}`、`user_message_duplicate_total`、`event_recovery_total`。
- `hook_duration_seconds{hook}`、`hook_failure_total{hook}`。

## 19. 兼容性、版本与发布策略

1. `ftre-agent` 的公共契约使用独立版本号；破坏性字段删除必须升级 major，新增字段默认可选。
2. Session/WS 的 `UserMessageEvent` 采用向后兼容新增字段；旧客户端至少能渲染 `content`，新客户端使用 `previous_assistant_message_id` 完成边界展示。
3. 不采用双写、双读或旧/新 Owner 并存的灰度方式；迁移在一个 Composition 版本内原子切换，旧入口同时删除。
4. Runtime Factory 与 AgentService 通过显式 `name/version/capabilities` 协商，缺少必需能力时启动失败，不运行半残 Agent。
5. 任何 Session 恢复都保存 `agent_package_version`、`runtime_factory_version`、`profile_snapshot_hash` 和 `contract_version`；不兼容时输出可操作错误。

## 20. 安全与权限边界

- AgentService 只能使用创建时传入的 `AgentConfig`；不能根据用户消息动态扩大 Tool policy。
- Profile 解析必须限制文件根目录，拒绝通过路径穿越读取其他用户/项目配置。
- Inbox Item 的 `source`、session、agent 归属必须在 admit 时校验；claim 时再次校验，防止跨 Session 投递。
- UserMessageEvent 的 metadata 不能放权限令牌、Cookie、API key 或完整附件二进制。
- `cancel`、`dispose`、`resume` 按调用者权限校验；失败只返回安全错误，不泄露其他 Agent 的 Session 内容。

## 21. 分阶段实施任务与交付物

### F35.1 AgentService Owner 分离

- 交付 `ftre_agent.plugin`、单一 `AgentService`、Factory registration。
- 删除 Runtime Plugin 中的 Service 构造、provide 和第二 Registry。
- 交付 Owner 架构测试和 Composition 启动日志测试。

### F35.2 公共契约冻结

- 交付 `contracts.py/events.py/hooks.py` 的 typed models、错误码、状态枚举和版本字段。
- 交付 fake RuntimeFactory、fake AgentEventSink 和独立 Package 测试。
- 交付 API 破坏性变更清单；禁止下一阶段再隐式加字段。

### F35.3 Profile/Prompt 边界

- 交付 `AgentProfileService.resolve`、优先级、来源 trace、snapshot hash 和结构化配置错误。
- 迁移 `src/ftre/services/agent/profile` 到 `agent_profile`，删除旧 Owner/桥接。
- 交付 Profile 与 Prompt/Tool policy 隔离测试。

### F35.4 Inbox admission/claim

- 交付 `PendingItem`、lease、reservation、CAS claim、defer/requeue/dead-letter。
- 删除 `InboundMessage` 进入 Agent 的路径和固定 busy polling。
- 交付并发 worker、Runtime crash、Gateway restart 的恢复测试。

### F35.5 UserMessage 边界

- 交付 stop-decision → before-reasoning → UserMessageEvent 的完整链路。
- 交付 Event id/message id/sequence 幂等记录与 Session Projection immediate close。
- 交付 next-step/next-turn 分流和客户端实时流回归测试。

### F35.6 收尾与门禁

- 交付架构扫描、clean install、全量 pytest/ruff、无旧引用证明。
- 更新本 PRD 勾选项、F35 TODO 子任务和变更记录。
- 删除迁移脚本、临时兼容导出、废弃注释和空目录；只保留终局结构。

## 22. 需求到测试的追踪矩阵

| 需求 | 必须存在的测试 |
|---|---|
| FR1–FR4 | `test_agent_service_owner.py`、`test_runtime_factory_registration.py`、clean import test |
| FR5、FR16 | `test_agent_state_machine.py`、`test_cancel_shutdown_resume.py` |
| FR6–FR7 | `test_profile_precedence.py`、`test_prompt_tool_policy_boundary.py` |
| FR8–FR9 | `test_inbox_reservation_race.py`、`test_inbox_recovery.py` |
| FR10–FR11 | `test_hook_order_and_failure_policy.py` |
| FR12–FR14 | `test_user_message_boundary.py`、`test_projection_closes_assistant.py` |
| FR15 | `test_llm_error_layering.py`、`test_inbox_run_result_mapping.py` |
| AC14 | `test_no_legacy_agent_owner.py`、architecture import scan |
| AC15 | CI 中 `pytest`、`ruff`、`git diff --check`、clean package install |

## 23. 完成定义（Definition of Done）

F35 只有在以下条件全部满足时才能将 TODO 标记为 `done`：

- [x] 1. PRD 状态已经历 `草稿 -> 评审 -> approved -> 开发中 -> 已验收`，每次状态变化有日期和理由。
- [x] 2. `ftre-agent` 是唯一 `agents` Service Owner；Runtime 仅 Factory Provider；Host Composition 只加载一次。
- [x] 3. Agent 公共 API 不含 `InboundMessage`/Queue/Channel/Repository；Profile、Tool、LLM、Inbox 依赖方向通过架构扫描。
- [x] 4. busy、cancel、error、restart、resume、lease recovery 具有可重复的自动化测试，且不会丢 Inbox 项或产生幽灵 UserMessage。
- [x] 5. `next-step` 产生一次真实 UserMessageEvent；旧 Assistant 在事件投影时立即关闭；Core Memory、Session、Bus、客户端 message id/sequence 一致。
- [x] 6. `next-turn` 使用新 Run；`ContinueTurn` 不被当作可见用户消息；错误/取消不错误续跑。
- [x] 7. F30 的 LLM Retry/Fallback、F34 的 ToolService 仍是唯一 Owner，没有复制实现。
- [x] 8. 旧 `src/ftre/services/agent` Owner、桥接、兼容导出、临时脚本和空目录已删除；全局搜索和 clean install 无陈旧引用。
- [x] 9. `python -m pytest -q`、`python -m ruff check src tests packages`、架构门禁和 `git diff --check` 全部通过。
- [x] 10. 本 PRD 的 FR/AC 勾选项、TODO F35 子任务、迁移矩阵和变更记录与实际代码一致。

## 24. 变更记录

| 日期 | 变更 | 理由与受影响验收 |
|---|---|---|
| 2026-08-29 | 修正崩溃恢复语义：`InboxRepository` 不再把旧 owner 的 inflight lease 回排到 pending；`InboxService.start()` 只加载队列快照，不自动启动 worker，必须由新 admission 或显式 `resume_pending()` 唤醒。`AgentLoop` 依据 Session 中持久化的 Assistant `request_id/run_id` 做幂等短路；`SessionProjection` 拒绝向已终态 Assistant 追加重放事件。 | 电脑/客户端异常退出后，原请求已经产生 Assistant 输出时再次恢复会造成重复执行和重复 Resume 文本。FR9、FR16、AC13 重新核验：未领取 pending 保留但不自动发送，旧 inflight 不重投，同一 request 不创建新 Turn，终态消息不再追加。 |
| 2026-08-29 | 增加暂停边界：`AgentRunResult.paused` 明确表示权限挂起而非自然完成；`AgentService` 将 Session 保持为 `paused`，`InboxService` 暂停消费后续队列，只有确认恢复并正常结束后才唤醒 worker。 | 修复权限确认暂停后队列被误消费。FR10、FR16、AC13 重新核验：paused 不消费队列，confirmation 完成后才恢复唯一的队列消费时机。 |
