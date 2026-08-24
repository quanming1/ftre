# PRD-F15：Hook 语义收敛与生命周期安全

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F15 |
| 名称 | Hook 语义收敛与生命周期安全 |
| 文档版本 | 第二版（Host-only 渐进收敛） |
| 状态 | 开发中 |
| 创建日期 | 2026-08-24 |
| 定稿日期 | 2026-08-24 |
| 验收日期 | — |
| 前置阶段 | F14（轻内核 + Plugin-first 最终目标架构） |
| 实施边界 | 只修改 `E:\ftre` Host 与仓内 `packages/`；不修改 `E:\ftre-agent-core`、Desktop 或其他仓库 |
| 后续阶段 | Core Tool Hook 4→2 与 `agent/turn-stopping` 命名治理另立阶段（暂定 F16 / Core C3），不阻塞 F15 |
| 关联文档 | `AGENTS.md`、`docs/TODO.yaml`、`PRD-F6`、`PRD-F7`、`PRD-F12`、`PRD-F14`、`E:\ftre-agent-core\docs\prd\PRD-C2-agent-before-reasoning-hook.md` |
| 实施提示词 | `docs/execution/prompts/F15/`（F15 七批）；`docs/execution/prompts/F16-C3/`（后续终局阶段预案） |
| 设计参考 | [Pi Extension API](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)、[oh-my-pi Hook 文档](https://github.com/can1357/oh-my-pi/blob/main/docs/hooks.md)、[pi-yaml-hooks](https://pi.dev/packages/pi-yaml-hooks) |

## 1. 背景与审计结论

F6/F7 建立了类型化 HookSpec、Cordis 作用域、Waterfall、失败策略和 in-flight
排空；F11/F12 又通过 Hook 将 Compaction、Inbox 从 Agent Runtime 解耦。机制层方向
是正确的，但 F14 收尾后，公开 Hook 面仍保留了重构过程中“先提供扩展点、以后再找
消费者”的中间设计。

当前代码共有 **29 个唯一 Hook 名称**：

| Owner | 数量 | 当前 Hook |
|---|---:|---|
| Agent Core / Tool | 4 | `tools/pre-execute`、`tools/execute`、`tools/post-execute`、`tools/result` |
| Agent Core / LLM | 1 | `llm/stream` |
| Agent Core / Agent | 2 | `agent/before-reasoning`、`agent/turn-stopping` |
| ftre Agent | 10 | `agent/before-turn`、`agent/after-turn`、`agent/request`、`agent/request-error`、`agent/turn-stopped`、`agent/created`、`agent/disposed`、`agent/error`、`agent/session-start`、`agent/status` |
| Session | 4 | `session/created`、`session/disposed`、`session/event`、`session/flush` |
| Messaging | 1 | `messaging/inbound` |
| System Prompt | 1 | `system-prompt/assemble` |
| Inbox Package | 6 | `inbox/before-claim`、`inbox/inserted`、`inbox/claimed`、`inbox/discarded`、`inbox/changed`、`inbox/status-changed` |

审计确认以下问题：

1. `agent/error`、`agent/session-start`、`agent/status` 只有定义和导出，没有任何发布点；
   Plugin 可以成功注册，但永远不会收到调用。
2. `agent/turn-stopped` 与 `agent/after-turn` 都在一次 Agent Turn 收尾阶段发布，前者没有
   生产监听者，且与控制型 `agent/turn-stopping` 名称过于接近。
3. `agent/request` 实际只转换 `AgentConfig`，名称却可能被理解为用户请求、HTTP 请求或
   LLM 请求；当前没有生产监听者，并与 Prompt/LLM 边界部分重叠。
4. Inbox 每次 mutation 同时发布 `inserted/claimed/discarded` 和 `changed`；只有
   `inbox/changed` 被 WebSocket 使用，三个细粒度 Hook 没有生产监听者。
5. `session/event` 在事实投影后发布，但同一事实已经由 MessageBus 下行；当前没有生产
   监听者。`session/flush` 也没有生产监听者，现有 `flush()` 只是分发空屏障。
6. Tool 管线拆成四个公共 Hook，但当前没有生产 Plugin 监听其中任何一个；相比常见的
   “执行前控制 + 执行后变换”模型，存在继续收敛的价值。不过这些契约由独立
   `ftre-agent-core` 仓库拥有，必须通过 Core 自己的 PRD、版本和回归测试治理，不能为了
   F15 的数字目标把跨仓库迁移强塞进同一交付批次。
7. `HookMode.EMIT` 会立即返回，并将异步 listener 放入 detached Task；但当前
   `session/disposed` 被 Inbox 用于必要清理，`inbox/changed/status-changed` 被 WebSocket
   用于权威状态推送。这些关键异步行为没有被发布者真正等待，存在清理未完成、状态乱序和
   Plugin unload 竞态。
8. 一次 Hook 注册同时可能由 `Context.on`、HookRuntime companion Effect 和 Plugin 手工
   `ctx.effect(receipt.dispose)` 管理；`context` 缺省时监听器又会错误绑定根 Context，迫使
   Plugin 手工补清理。生命周期 API 可用，但不够单一、直观。
9. 作用域同时由 `HookSpec.scope`、`context`、仅供诊断的字符串 `scope` 和
   `global_listener` 表达，Plugin 作者很难判断哪一个真正参与匹配和卸载。

### 1.1 总目标

F15 完成后，ftre 的公共 Hook 必须满足一句话：

> 只在长期稳定、确有零个或多个 Plugin 消费价值的语义边界发布 Hook；名称直接表达
> 时机和允许动作；关键异步行为必须被等待；注册只归属于一个 Cordis Fiber Owner。

F15 将 Host 自有 Hook 从 22 个收敛到 **10 个**，全系统公共 Hook 从 29 个收敛到
**17 个**。Core 当前 7 个 Hook 在本阶段原样保留；Host、Inbox、Compaction 和内置
Plugin 在同一个 ftre feature 分支中完成切换，不保留 Host 旧名称 alias、双发或
compatibility bridge。

终局仍以 **15 个 Hook** 为候选方向，但只有后续 Core 审计证明四段 Tool Hook 可以安全
收敛为两段、`agent/turn-stopping` 改名收益大于生态迁移成本后才能进入正式 PRD。F15
不得用“终局可能还会改”为理由延迟当前 Host 债务清理。

### 1.2 非目标

- 不重写 Cordis Events、Context、Fiber、Effect 或依赖解析算法。
- 不把 MessageBus、SessionEvent、Inbox 队列或公开 Service 替换成 Hook。
- 不为了模仿 Pi 而照搬其 ExtensionContext、UI、Command 或 SessionManager 聚合对象。
- 不增加数字优先级、全局 Hook 注册表、Service Locator 或第二套生命周期状态机。
- 不修改 Desktop wire contract；`session/queue`、`session/status`、USER_MESSAGE 和 ACK
  语义保持不变。
- 不增加新的业务 Plugin；本阶段只收敛既有 Hook 机制和消费关系。
- 不保留本阶段删除或改名的 Host Hook 兼容入口；仓库内 Inbox/Compaction 与 Host 原子迁移。
- 不修改、改名、删除或复制 `E:\ftre-agent-core` 的 7 个 Hook 契约；Core 治理另立 PRD、
  feature 分支和 PR。

## 2. 统一术语与 Hook 准入门禁

### 2.1 执行层级

F15 统一使用以下四层术语：

```text
Run             一条 InboundMessage 驱动的一次完整 AgentService 执行
└─ Reasoning    一次真实 LLM 推理调用前后的 Core Step
   └─ Tool Call 一次具体工具调用

Session / Inbox 是 Run 外部的持久事实与未来输入 Owner
```

禁止再用没有限定词的 `request`、`turn`、`event`、`status` 表达多个层级。确需使用
`turn` 时必须在类型和文档中明确它表示 Run 还是 LLM Step；F15 的公共名称统一使用
`run` 和 `reasoning`，避免与外部 Agent 框架的 Turn 定义冲突。

### 2.2 四类 Hook

| 类型 | 模式 | 是否等待 | 是否允许改变主流程 | 失败语义 |
|---|---|---:|---:|---|
| 控制/变换 | `WATERFALL` | 是 | 是，返回类型化 Decision/替换值 | 默认 `PROPAGATE` |
| 串行维护 | `SERIAL` | 是 | 不回滚已提交事实 | 按契约 `OBSERVE` 或 `PROPAGATE` |
| 并行通知 | `PARALLEL` | 是 | 否 | `OBSERVE`，记录全部失败 |
| 遥测广播 | `EMIT` | 否 | 否 | 只允许日志/指标等可丢失行为 |

任何需要删除数据、释放资源、推送权威客户端状态、推进持久化屏障或保证顺序的异步
listener 都不得注册到 `EMIT` Hook。

### 2.3 新增 Hook 的六项门禁

新增公共 Hook 前必须同时证明：

1. 有唯一语义 Owner 和明确发布点；
2. 至少有一个真实 Plugin 消费者，或属于必须公开的稳定扩展边界；
3. 时机独立于私有 helper、临时 DTO 和目录结构；
4. Payload、Result、默认行为、失败策略和取消语义可类型化；
5. 零监听器时主流程行为完整且可测试；
6. 不能用一次普通 Service 调用、MessageBus 事实通知或 Tracer 更清晰地表达。

不满足门禁的能力保持 Owner 私有方法，不预留空 Hook。

## 3. F15 目标 Hook 表

### 3.1 主执行链

```text
messaging/route
        │
        ▼
inbox/before-claim
        │
        ▼
agent/before-run
        │
        ├─ system-prompt/assemble
        │
        ▼
agent/before-reasoning          # Core：每次真实 LLM Reasoning
        │
        ▼
llm/stream                      # Core
        │
        ▼
tools/pre-execute
        │
        ▼
tools/execute → Tool Body → tools/post-execute → tools/result
        │
        ▼
agent/turn-stopping             # Core：自然停止前的 continuation 决策
        │
        ▼
agent/after-run
```

异常支线：

```text
Agent Run 失败
  → agent/run-error
  → None（保留原错误）或 RetryRun（有界、带 progress token）
```

持久事实通知：

```text
session/created
session/disposed
inbox/changed
inbox/status-changed
```

### 3.2 精确契约

| Hook | Owner | 模式 | Scope | Failure | 输入 → 输出 | 语义 |
|---|---|---|---|---|---|---|
| `messaging/route` | MessagingService | WATERFALL | GLOBAL | PROPAGATE | `BusMessage → IngressResult \| None` | Command/Inbox 按注册顺序接管入站消息 |
| `session/created` | SessionService | PARALLEL | GLOBAL | OBSERVE | `SessionLifecyclePayload → None` | Session 创建事实已提交；等待所有观察者完成 |
| `session/disposed` | SessionService | PARALLEL | GLOBAL | OBSERVE | `SessionLifecyclePayload → None` | Session 删除事实已提交；等待 Inbox 等 Owner 清理 |
| `agent/before-run` | AgentService | WATERFALL | AGENT | PROPAGATE | `BeforeRunPayload → AllowRun \| RejectRun` | 已交付输入进入 Agent Run 前的准入策略 |
| `agent/after-run` | AgentService | SERIAL | AGENT | OBSERVE | `AfterRunPayload → None` | Run 已有终态后的有序维护；失败不改写已完成结果 |
| `agent/run-error` | AgentService | WATERFALL | AGENT | PROPAGATE | `RunErrorPayload → RetryRun \| None` | 完成可证明恢复后请求有界重试 |
| `agent/before-reasoning` | Agent Core | WATERFALL | AGENT | PROPAGATE | `BeforeReasoningPayload → BeforeReasoningResult` | 每次真实 LLM 推理前贡献普通消息；保持 C2 语义 |
| `agent/turn-stopping` | Agent Core | WATERFALL | AGENT | PROPAGATE | `TurnStoppingPayload → StopTurn \| ContinueTurn` | Core 自然停止前决定结束或有界 continuation；F15 不改名 |
| `llm/stream` | Agent Core | WATERFALL | AGENT | PROPAGATE | `LLMStreamPayload → AsyncIterator` | 模型路由、包装、遥测和 stream continuation |
| `tools/pre-execute` | Agent Core | WATERFALL | AGENT | PROPAGATE | `ToolPreExecutePayload → ToolAllow \| ToolDeny \| ToolArguments` | 执行前阻止或替换参数；F15 原样保留 |
| `tools/execute` | Agent Core | WATERFALL | AGENT | PROPAGATE | `ToolExecutePayload → ToolExecutionResult` | 包裹实际 Tool invoke；F15 原样保留 |
| `tools/post-execute` | Agent Core | WATERFALL | AGENT | PROPAGATE | `ToolPostExecutePayload → ToolExecutionResult` | 执行后替换最终结果；F15 原样保留 |
| `tools/result` | Agent Core | EMIT | AGENT | OBSERVE | `ToolResultPayload → None` | 最终结果观察；F15 原样保留 |
| `system-prompt/assemble` | SystemPromptService | WATERFALL | AGENT | PROPAGATE | `PromptAssemblyPayload → PromptAssembly` | 结构化组装 System Prompt |
| `inbox/before-claim` | ftre-inbox | WATERFALL | GLOBAL | PROPAGATE | `BeforeClaimPayload → EnterClaim \| RejectClaim` | pending 仍未领取时执行压缩等门控 |
| `inbox/changed` | ftre-inbox | PARALLEL | GLOBAL | OBSERVE | `InboxChangedPayload → None` | 队列事实已提交；等待协议适配器读取权威快照 |
| `inbox/status-changed` | ftre-inbox | PARALLEL | GLOBAL | OBSERVE | `InboxStatusPayload → None` | Inbox 自有 blocked/idle 状态变化；与快照独立 |

## 4. 当前到目标的迁移表

| 当前 Hook | 目标 | 决策理由 |
|---|---|---|
| `tools/pre-execute` | 保留 | Core 契约；是否与其他 Tool Hook 合并由后续 Core 阶段决定 |
| `tools/execute` | 保留 | Core around 契约；F15 不跨仓重写 Tool 执行链 |
| `tools/post-execute` | 保留 | Core 结果变换契约；F15 不改名 |
| `tools/result` | 保留 | Core 遥测观察契约；F15 只保证 Host Runtime 的 EMIT 语义不被业务清理滥用 |
| `llm/stream` | 保留 | 唯一模型调用 around 边界，语义清晰 |
| `agent/before-reasoning` | 保留 | next-step 和每次真实 LLM Step 的稳定边界 |
| `agent/turn-stopping` | 保留 | Core 契约；改名另立 Core 阶段，不与 Host `turn-stopped` 混为同一问题 |
| `agent/before-turn` | `agent/before-run` | 明确一条 InboundMessage 的完整运行边界 |
| `agent/after-turn` | `agent/after-run` | 与 before-run 成对；改为 awaited SERIAL 维护 |
| `agent/request` | 删除 | 实际只改 AgentConfig、无消费者、名称歧义 |
| `agent/request-error` | `agent/run-error` | 当前处理整个 Core Run 错误，不限于某类 request |
| `agent/turn-stopped` | 删除 | 与 after-run 重叠且无生产消费者 |
| `agent/created` | 删除 | 实际是 AgentRegistry scope identity，不是 Run 生命周期；当前无消费者 |
| `agent/disposed` | 删除 | 同上；scope 销毁保持 AgentService 私有生命周期 |
| `agent/error` | 删除 | 无发布点 |
| `agent/session-start` | 删除 | 无发布点，且与 Session Owner 重叠 |
| `agent/status` | 删除 | 无发布点；状态由 AgentService/Inbox 和 wire protocol 负责 |
| `session/created` | 保留，改 PARALLEL | 已提交事实通知，但必须等待异步观察者 |
| `session/disposed` | 保留，改 PARALLEL | Inbox 清理必须在 dispatch 返回前完成 |
| `session/event` | 删除 | 无生产消费者；MessageBus 已发布同一持久事实 |
| `session/flush` | 删除 | 当前无消费者且不是实际持久化实现；未来有多 Store 屏障再立项 |
| `messaging/inbound` | `messaging/route` | 当前是控制路由链，不是普通观察事件 |
| `system-prompt/assemble` | 保留 | Owner、输入输出和消费者清晰 |
| `inbox/before-claim` | 保留 | Compaction 解耦所需稳定门控 |
| `inbox/inserted` | 删除 | 无生产消费者；changed 已覆盖权威事实变化 |
| `inbox/claimed` | 删除 | 同上 |
| `inbox/discarded` | 删除 | 同上 |
| `inbox/changed` | 保留，改 PARALLEL | WebSocket 权威快照通知，必须等待 |
| `inbox/status-changed` | 保留，改 PARALLEL | WebSocket 状态通知，必须等待 |

迁移完成后，源码、测试、README、PRD 示例和仓内 Package 中不得出现本表已删除或改名的
**Host 旧名称**及兼容常量。第 3.2 节明确保留的 7 个 Core 名称不属于旧名清理目标。

## 5. Hook Runtime 注册与生命周期方案

### 5.1 唯一注册 Owner

Plugin 注册必须显式传入当前 Cordis Context；该 Context 的 Fiber 是监听器生命周期的
唯一 Owner：

```python
receipt = ctx.hook_runtime.register(
    SPEC,
    listener,
    owner="ftre-inbox",
    context=ctx,
    all_agent_scopes=True,
)
```

规则：

- `context` 对 Plugin 注册为必填；只有 Composition 根诊断可以使用 root Context；
- `register()` 自己绑定 Cordis Effect 并等待 in-flight listener 排空；
- Plugin 不再额外执行 `ctx.effect(lambda: receipt.dispose)`；
- receipt 只服务“Plugin 仍存活时主动提前取消”，不是常规 unload 的第二 Owner；
- 删除仅供诊断的字符串 `scope` 参数；诊断标签从 Context isolate 和 Owner 推导；
- `global_listener` 改为语义更明确的 `all_agent_scopes`；它只决定接收范围，不决定
  生命周期 Owner；
- 一个 listener 只能有一个 Cordis disposer 和一个 HookRuntime companion drain Effect。

### 5.2 Dispatch 语义

- `EMIT` 明确为同步触发 + 异步 listener detached，仅允许可丢失遥测；
- `PARALLEL` 必须启动全部 listener、等待全部完成，并按 OBSERVE 记录失败；
- `SERIAL` 必须按注册顺序逐个等待，维护 Hook 不使用 `next_`；
- `WATERFALL` 保持 continuation 语义；每个 listener 最多调用一次 `next_()`；
- dispatch 后的类型校验、取消检查和诊断不记录用户 payload；
- Plugin unload 先阻止新调用，再等待已进入 listener 归零。

## 6. Plugin 与 Package 迁移

### 6.1 ftre-inbox

- 监听 `messaging/route` 接管普通输入；
- 继续监听 `agent/before-reasoning` 原子消费 next-step；
- 监听 awaited `session/disposed` 删除本 Session Inbox；
- 发布 `inbox/before-claim`、`inbox/changed`、`inbox/status-changed`；
- 删除细粒度 mutation Hook 及其 DTO、dispatch、导出和测试；
- 所有监听器显式绑定 Package Plugin Context，不再手工重复 Effect。

### 6.2 ftre-compaction

- `agent/after-turn` 迁为 `agent/after-run`；
- `agent/request-error` 迁为 `agent/run-error`；
- 保留 `inbox/before-claim`；
- 删除 receipt 二次 Effect；
- 取消、overflow recovery、progress token、pending 保留和轮后预压缩行为不变。

### 6.3 Command、WebSocket、Session Title

- Command 监听 `messaging/route`，仍优先于 Inbox；
- WebSocket 监听 awaited `inbox/changed/status-changed`，下行协议不变；
- Session Title 继续监听 `system-prompt/assemble`；
- 三者全部只依赖公开 HookSpec，不 import HookRuntime 私有注册结构。

### 6.4 后续 Core 候选（不属于 F15）

F15 只记录以下审计输入，不据此修改依赖版本、Core 源码或 ftre 的 Core Hook 调用点：

- 评估 `tools/pre-execute`、`tools/execute`、`tools/post-execute`、`tools/result` 是否可收敛为
  `tool/before`、`tool/after`；
- 评估 `agent/turn-stopping` 是否应改名为 `agent/stop-decision`；
- 证明 Tool around、权限确认、取消、错误归一化、并发和结果观察不会因合并丢失；
- 通过独立 Core PRD、TODO、feature 分支、pytest、ruff、wheel、版本发布和 ftre 配对验证。

上述候选预计作为 F16 / Core C3 另行立项。未立项、未 approved 前，7 个 Core Hook 都是
F15 的稳定输入，架构测试必须防止 F15 误删或私自改名。

## 7. 功能需求

### 7.1 基线与门禁

- [x] **FR1：Hook 事实清单。** 由 AST/运行时快照生成当前 HookSpec、发布者、生产监听者、
  模式、Scope、失败策略和生命周期 Owner；不得靠手写列表冒充事实源。
- [x] **FR2：目标清单唯一。** 架构测试断言公共 Hook 名集合与第 3.2 节 17 个名称完全一致，
  不允许额外旧名、alias 或未登记 Hook。
- [x] **FR3：发布者门禁。** 每个 HookSpec 至少有一个真实发布点；删除“能注册但永不触发”的
  幽灵 Hook。
- [x] **FR4：消费价值门禁。** 新增 Hook 必须在 PRD 记录真实消费者或稳定扩展价值；没有
  消费者的内部时机保持私有方法。

### 7.2 Runtime 语义

- [x] **FR5：EMIT 限界。** 生产业务 Hook 不得使用 EMIT 执行异步清理、权威状态推送或
  持久化屏障；架构测试扫描并拒绝此类组合。
- [x] **FR6：awaited lifecycle。** `session/created/disposed`、`inbox/changed/status-changed`
  使用 awaited 模式；dispatch 返回时对应 listener 已结束。
- [x] **FR7：唯一 Effect Owner。** Hook 注册自动绑定传入 Context Fiber；生产 Plugin 不再为
  receipt 注册第二个 dispose Effect。
- [x] **FR8：Context 必填。** Plugin 级 `register()` 缺少 Context 时直接失败；根 Context
  注册仅允许显式内部 API，不能由业务 Plugin 静默使用。
- [x] **FR9：作用域收敛。** 删除诊断字符串 scope；将 `global_listener` 改为
  `all_agent_scopes`，并证明它不改变生命周期 Owner。
- [x] **FR10：in-flight 清理。** unload/restart 在 listener 执行中发生时，阻止新调用并等待
  现有调用排空；取消和异常路径无悬挂 Task。

### 7.3 Host 与 Package Hook

- [x] **FR11：Agent Hook 收敛。** 按迁移表删除幽灵/重复 Hook并迁移为
  `before-run/after-run/run-error`；Agent Runtime 不识别 Compaction/Inbox 实现。
- [x] **FR12：Session Hook 收敛。** 只保留 awaited `session/created/disposed`；删除
  `session/event/flush` 及没有消费者的 DTO、默认函数、测试和文档。
- [x] **FR13：Messaging 路由。** `messaging/route` 保持 Command-first、Inbox-second 的
  Waterfall 语义；无人处理时返回稳定 capability error。
- [x] **FR14：Inbox Hook 收敛。** 只保留 before-claim、changed、status-changed；删除三种
  细粒度 mutation Hook，队列持久化、revision 和客户端快照行为不变。
- [x] **FR15：Compaction 迁移。** 压缩包只监听 after-run、run-error、before-claim；禁用或卸载
  后基础 Agent Run 正常，pending 不丢失。
- [x] **FR16：内置 Plugin 迁移。** Command、WebSocket、Session Title 使用目标 Hook 名和唯一
  Fiber Effect；Plugin unload/restart 无 listener、Task 或闭包残留。

### 7.4 Core 边界冻结

- [x] **FR17：Core 契约不变。** F15 不修改 Core 依赖版本和 7 个 Core Hook 的名称、Spec、Payload、
  Result 或调用顺序；架构测试明确区分“保留的 Core Hook”和“删除的 Host Hook”。
- [x] **FR18：跨仓库隔离。** ftre 不复制 Core Hook 类型、不添加临时 adapter、不通过本地
  `sys.path` 或未提交 Core worktree 获得测试通过；后续 Core 候选只记录在本 PRD 第 6.4 节。

### 7.5 清理、文档与诊断

- [x] **FR19：无兼容层。** 删除 Host 旧常量、旧 Payload、旧导出、旧测试和旧文档示例；不保留
  deprecated alias、双发或 adapter。
- [x] **FR20：诊断可理解。** Hook snapshot 输出 name、owner、mode、真实 Context scope、顺序、
  active calls 和 disposed；不显示已删除 Hook 或用户 payload。
- [x] **FR21：中文文档。** Kernel README 和各 Owner hooks.py 解释目标 17 个 Hook 的时机、
  输入输出、失败语义、是否等待及最小 Plugin 示例。
- [x] **FR22：工程卫生。** 删除死代码、未使用 import、空目录、生成缓存和重复 Effect；ruff、
  diff check、架构扫描无 allowlist 扩张。
- [ ] **FR23：最终验收。** ftre 全量测试、两个 Package 独立测试与 wheel、Gateway smoke、
  Plugin unload/restart 和 ftre CI 全部通过，执行报告记录真实命令和结果。

## 8. 非功能需求

- **确定性**：相同 Composition 的 listener 顺序稳定；不依赖文件系统枚举、哈希或 detached
  Task 调度顺序。
- **可理解性**：开发者只看 Hook 名就能判断它属于 Run、Reasoning、Tool、Session 还是 Inbox；
  不使用裸 `request/event/status`。
- **可卸载性**：Plugin unload 后无新调用进入，in-flight 调用按契约排空，listener 从 snapshot
  消失。
- **安全性**：Tool 拒绝不能被后续 listener 重新放行；Argument/Result 使用冻结快照或复制值；
  Hook 诊断不记录完整 Prompt、Tool 参数和密钥。
- **性能**：零监听器路径不创建 Task；before-reasoning、现有 Tool 四段 Hook 和 llm/stream 热路径
  不进行全局扫描。
- **兼容性**：本阶段 Host Python Hook API 明确不兼容；Core 7 个 Hook、Desktop wire、Session
  数据、Inbox 持久格式和 Command 文本协议保持兼容。
- **包边界**：ftre Host 不 concrete import Inbox/Compaction 私有实现；Package 只依赖目标公开
  Hook/Service。

## 9. 分批任务

| 任务 | 内容 | 停止条件 |
|---|---|---|
| F15.1 | 当前 Hook/发布者/监听者/Effect/Scope AST 基线和架构门禁 | 29 个现状有代码证据；F15 目标 17 个清单冻结 |
| F15.2 | HookRuntime awaited 语义、Context 必填、唯一 Effect Owner | 生命周期/并发专项通过，无重复 disposer |
| F15.3 | Host Agent/Messaging Hook 删除和改名 | 幽灵 Hook、request、turn-stopped 清零 |
| F15.4 | Session/Inbox Hook 收敛与 WebSocket 顺序修复 | Session 清理和队列推送均被 await，旧 mutation Hook 清零 |
| F15.5 | Compaction、Inbox、Command、Session Title 消费迁移 | 可选 Plugin load/unload/restart 全通过 |
| F15.6 | 取消、异常、in-flight、并发和状态顺序测试 | 无丢消息、重复消费、悬挂 Task 或乱序快照 |
| F15.7 | Package 独立发行与 Core 边界回归 | 两个 Package wheel/洁净安装通过；Core 7 Hook 契约未变化 |
| F15.8 | 文档、诊断、旧引用、缓存和死代码清理 | 旧 Hook 名全盘扫描为零 |
| F15.9 | 全量验收、执行报告、PRD/TODO/CHANGELOG 收尾 | ftre CI 绿色、工作树干净、分批提交完成 |

## 10. 验收标准

- [x] **AC1：精确清单。** AST 和运行时 snapshot 均只出现第 3.2 节 17 个 Hook；其中 Host 10 个、
  Core 7 个，Owner 归属不可混淆。
- [x] **AC2：幽灵 Hook 清零。** 每个 HookSpec 均有发布者；旧 Agent 幽灵 Hook 全盘搜索为零。
- [x] **AC3：注册 API。** 所有生产 listener 显式绑定 Plugin Context，Plugin 源码没有
  `ctx.effect(...receipt.dispose...)` 二次生命周期注册。
- [x] **AC4：Session 清理等待。** 人工阻塞 Inbox 的 session/disposed listener 时，Session
  dispatch 不得提前返回；释放后完整清理。
- [x] **AC5：WebSocket 顺序。** 连续 Inbox mutation/status 变化产生顺序一致的权威快照和状态，
  无 detached Task 迟到覆盖新状态。
- [x] **AC6：in-flight unload。** 控制/维护 listener 执行中卸载 Plugin，unload 等待排空；卸载后
  再 dispatch 不进入旧 listener。
- [x] **AC7：Agent Run。** before-run 拒绝不持久化/执行错误输入；after-run 在成功、错误、取消后
  恰好执行一次。
- [x] **AC8：错误恢复。** run-error 只有 progress token 前进且未取消时允许一次有界重试；重复 token
  和恢复失败保留原错误。
- [x] **AC9：Reasoning。** next-step 在首次、Tool 后和 continuation 后的 before-reasoning 原子消费，
  不丢失、不重复。
- [x] **AC10：Core Tool 契约冻结。** `tools/pre-execute`、`tools/execute`、`tools/post-execute`、
  `tools/result` 四个名称及 Host dispatch 兼容测试保持通过，F15 不引入 `tool/before/after` 半迁移。
- [x] **AC11：Core Agent 契约冻结。** `agent/before-reasoning`、`agent/turn-stopping` 与
  `llm/stream` 保持现状；默认 Stop、Continue、有界 continuation 和 next-step 回归通过。
- [x] **AC12：跨仓库隔离。** `E:\ftre-agent-core` 无 F15 修改；ftre 源码没有复制 Core Hook DTO、
  compatibility adapter、临时 `sys.path` 或本地路径依赖。
- [x] **AC13：Compaction。** before-claim、after-run、run-error 三条路径行为保持；卸载包后 Agent
  基础流程无条件分支和 no-op fallback。
- [x] **AC14：Inbox。** admission、next-turn/next-step、claim、discard、恢复、容量、取消和权威快照
  测试通过；旧细粒度 mutation Hook 不存在。
- [x] **AC15：最小 Composition。** 未安装 Inbox/Compaction 时 Host 可 import、Composition 可启停、
  直接 Agent Run 可执行。
- [x] **AC16：Package。** Inbox/Compaction 独立测试、wheel build、洁净安装和 entry point discovery
  通过，wheel 不夹带缓存或 Host 私有源码。
- [x] **AC17：质量门禁。** `python -m pytest -q`、`python -m ruff check --no-cache src tests packages`、
  `git diff --check` 全部通过。
- [x] **AC18：Gateway smoke。** Config、HTTP health、WebSocket attach、消息 admission、Agent 回复、
  queue/status、取消和正常 shutdown 通过。
- [ ] **AC19：ftre CI。** F15 GitHub Actions 成功；声明的 Core 版本和两个本地可选 Package 可在
  洁净 runner 安装，不依赖其他仓库 dirty worktree。
- [ ] **AC20：收尾一致。** PRD、TODO、CHANGELOG、执行报告、注释和实际 Hook 清单一致；工作树干净，
  所有代码按职责分批提交。

## 11. 测试计划

### 11.1 Hook Runtime

- 模式：WATERFALL/SERIAL/PARALLEL/EMIT 的等待、顺序、异常和返回值；
- 生命周期：Context/Fiber owner、restart、unload、重复 dispose、in-flight drain；
- Scope：单 Agent、父子 Agent、兄弟 Agent、同 id 重建和 all-agent listener；
- 诊断：顺序、Owner、active calls、失败脱敏和 disposed 清理。

### 11.2 Agent Core 边界回归

- 现有 tools/pre-execute 的允许、拒绝、参数替换、并发和取消；
- tools/execute around、tools/post-execute 结果变换、tools/result 观察的现有契约；
- before-reasoning 首次/Tool 后/continuation 后调用；
- turn-stopping 的 Stop/Continue/上限；
- llm/stream 的默认 continuation 和自定义 wrapper；
- ftre 架构扫描确认没有出现 `tool/before`、`tool/after`、`agent/stop-decision` 半迁移名称。

### 11.3 Host、Inbox 与 Compaction

- messaging/route 的 Command-first、Inbox fallback 和无人处理；
- before-run/after-run/run-error 正常、拒绝、错误、取消和重试；
- Session disposed 与 Inbox 清理等待；
- Inbox changed/status 与 WebSocket 顺序；
- Compaction 三个 Hook、pending 保留、overflow recovery 和卸载降级。

### 11.4 架构与洁净环境

- AST 断言目标清单、发布者、监听者和旧名清零；
- Host 不 import Package 私有实现，Core 不 import Host；
- 两个 Package 独立 wheel 和无包最小 Composition；
- Linux CI 与 Windows 本地 Python 3.12 均通过。

## 12. 风险与迁移纪律

| 风险 | 控制措施 |
|---|---|
| 为追求 15 个目标误改 Core | F15 冻结 7 个 Core Hook；后续变化必须另立 Core PRD 和 ftre 配对阶段 |
| 删除 Hook 破坏未登记外部插件 | 本阶段明确不兼容；发布说明列出迁移表，不保留 alias |
| EMIT 改 awaited 增加延迟 | 只改变必要清理/权威状态 Hook；增加耗时诊断和并发测试 |
| after-run 维护阻塞下一条消息 | SERIAL、有取消信号；慢 listener 有诊断，Compaction 保持现有互斥语义 |
| 暂缓 Tool 收敛导致债务被遗忘 | 第 6.4 节保留可执行审计输入；F15 验收后再决定是否立 F16/Core C3 |
| Inbox mutation 细节不可观察 | Queue snapshot/revision 是权威事实；审计需求走 Store/Trace，不恢复重复 Hook |

迁移期间允许 feature 分支内的 Host/Package 短暂不兼容，但禁止把只完成一半的 Host
契约合入 `develop`。Host 旧 Hook 不能与新 Hook 双发来换取“渐进兼容”，否则会产生
双执行和重复清理；Core 7 个 Hook 则必须全程保持可用。

## 13. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-24 | F15.1 基线建立并开始 F15.2：新增从真实 Spec 生成 29 项唯一清单的架构测试；生产注册改用 `all_agent_scopes`、不再传入诊断 scope，Plugin 删除 receipt 二次 Effect | 先把当前事实和生命周期 API 固定，避免后续 Hook 删除/改名时引入第二 Owner；Session/Inbox awaited 模式留给 F15.4 完成 |
| 2026-08-24 | 完成 F15.2/F15.3：Runtime 注册生命周期收敛；Agent Host 删除 lifecycle/request/turn-stopped 并迁移 before-run/after-run/run-error；Messaging inbound 改为 route；F15.3 后唯一快照 22 项 | 先清理无消费者和歧义的 Host Hook，不改变 Core 7 项；Session/Inbox 通知语义留到下一批验证 |
| 2026-08-24 | 完成 F15.4：Session 只保留 awaited created/disposed；Inbox 只保留 before-claim/changed/status-changed 并将权威通知改为 awaited PARALLEL；删除 Session event/flush 和 Inbox 三种细粒度 mutation | 消除重复事实通知和 detached 状态推送，保证 Session 删除清理、队列 revision 与 WebSocket snapshot 的顺序可等待 |
| 2026-08-24 | 完成 F15.5：Inbox、Compaction、Command、WebSocket、Session Title 全部迁移目标 Hook；生产扫描清零旧 Host 名、global_listener 和 receipt 二次 Effect | 让 Package/内置 Plugin 的实际消费者与 17 项目标表一致，卸载由 HookRuntime Fiber Owner 统一收尾 |
| 2026-08-24 | 完成 F15.6/F15.7：补齐 in-flight unload、Session dispose 失败和队列恢复回归；构建并洁净安装 Inbox/Compaction wheel，验证 Core 7 项冻结 | 在最终清理前证明取消/生命周期/Package 发行边界，避免把本地构建目录或 Core sibling 依赖带入验收 |
| 2026-08-24 | F15.8 本地清理与 F15.9 预验收：全量 486 passed、ruff/diff 通过、Gateway health/WebSocket attach/优雅关闭 smoke 通过，清除缓存和空生成物；AC19 保持未勾选，等待 feature push 后的 GitHub Actions | 诚实区分本地可复现证据与必须在远程洁净 runner 执行的 CI 门禁，不提前标记阶段已验收 |
| 2026-08-24 | 用户授权按 F15.1-F15.9 串行执行；PRD 由草稿进入开发中，FR/AC 保持未勾选，验收以执行证据为准 | 开始按第二版 Host-only 17 Hook 方案落地，禁止提前宣称完成 |
| 2026-08-24 | 增加 F15 七批执行提示词，并为后续 F16/Core C3 建立“先配对 PRD、后 Core、再 Host”的七批预案 | 将 PRD 转换为自包含、可验证、带注释/卫生/提交边界的 Agent 执行契约，避免一次性跨仓迁移和未发布 sibling 依赖 |
| 2026-08-24 | 第二版：F15 从跨仓 29→15 调整为 Host-only 29→17；冻结 Core 7 个 Hook，将 Tool 4→2 和 turn-stopping 改名移入后续 F16/Core C3 候选；同步重写 FR、任务、AC、测试与风险 | 第一版同时修改 HookRuntime、Host、两个 Package 和 Core，交付面过大且会让 Host 债务清理被 Core 发版阻塞；第二版保留终局方向，但先完成单仓可独立验收的收敛 |
| 2026-08-24 | 创建 F15 草稿：基于当前代码和 Pi/oh-my-pi 对照，冻结 29 → 15 Hook 收敛方向、EMIT awaited 边界、唯一 Effect Owner、跨仓库 Core C3 和 AC1-AC20 | F14 后 Hook 机制正确但公共事件面存在幽灵 Hook、重复完成通知、Inbox 双通知、Tool 过度分段及异步生命周期竞态；需要在继续扩展前先收敛语义 |
