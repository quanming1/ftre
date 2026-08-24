# ftre PRD 总览与跨阶段架构契约

> 本目录中的 PRD 是根据当前代码、测试和运行行为反推形成的。阶段 PRD 负责
> “做什么”和“如何验收”，本文负责跨阶段的术语、依赖和不可违反的边界。
>
> 如果本文与某个阶段 PRD 冲突，以最近一次经过「变更记录」修订的阶段 PRD 为准，
> 同时必须补回本文，避免同一事实在多个文档中漂移。

> **F12 更新（2026-08-23）**：下方早期 A/B 阶段中的 `SessionLane`、
> `MailboxStore`、`state.json.mailbox` 和 `mailbox_snapshot` 是历史实现记录，
> 不再是当前运行契约。当前队列 Owner 以
> [PRD-F12-session-inbox-protocol.md](PRD-F12-session-inbox-protocol.md) 为准：
> `packages/ftre-inbox` 拥有队列和 worker，`AgentService` 只执行
> `InboundMessage`。

> **F13 已验收（2026-08-23）**：`PRD-F13-plugin-first-kernel.md` 已落实
> Plugin-first：Kernel 只提供 Context、Plugin、Hook、Lifecycle 和 Diagnostics，业务能力由
> 唯一 Plugin/Service Owner 通过 Inject/Hook 协作；Queue → History 交接、Owner Plugin 路由
> 和可选能力降级均已验证。

> **F14 已验收（2026-08-24）**：
> [`PRD-F14-final-plugin-first-architecture.md`](PRD-F14-final-plugin-first-architecture.md)
> 是当前目录和 Owner 边界的终局契约。目标目录统一为 `app/kernel/services/plugins/packages`，
> `AgentLoop` 降为 AgentService 私有 Runtime，产品能力由 Builtin/External Plugin 管理，
> `ftre-inbox` 与 `ftre-compaction` 保持独立可选 Package。分批实施提示词及其历史执行顺序见
> [`docs/execution/prompts/F14/`](../execution/prompts/F14/README.md)。

> **F15 开发中（2026-08-24）**：
> [`PRD-F15-hook-surface-convergence.md`](PRD-F15-hook-surface-convergence.md)
> 负责收敛 F6-F14 演进后留下的 Hook 语义债务。第二版把交付面限定在 ftre Host 与仓内
> Package：Host Hook 22→10、全系统 29→17，Core 7 个 Hook 原样冻结；删除幽灵 Hook、
> 重复通知和 Host 旧名，修复 EMIT 承载关键异步清理的竞态，并让一次监听器注册只归属于
> 一个 Cordis Fiber Owner。Tool Hook 4→2 与停止决策改名留给后续独立 Core 阶段。
> F15 已按用户授权进入开发阶段。分批执行契约见
> [`docs/execution/prompts/F15/`](../execution/prompts/F15/README.md)；后续终局阶段的配对 PRD 与
> 跨仓执行预案见 [`docs/execution/prompts/F16-C3/`](../execution/prompts/F16-C3/README.md)。

## 当前运行契约（F12）

```text
WS/HTTP/Plugin
    → EventBus
    → AgentLoop（Command 旁路；普通输入交给 Inbox）
    → ftre-inbox.InboxService
        → next-step 全部候选 + next-turn 最多一条
        → inbox/before-claim
        → 原子 claim
        → AgentService.run(InboundMessage)
        → Agent Core / agent/after-turn
    → session/queue + session/status + session/event
```

当前所有权只有三条：

| Owner | 负责 | 不负责 |
|---|---|---|
| `ftre-inbox` | 双队列、持久化、容量、幂等、claim、worker、Queue wire | Agent 算法、Command 解析、Session 历史 |
| `AgentService` | 单条 `InboundMessage` 的 active Turn、取消和 Agent Hook | pending、QueueItem、capacity、queue snapshot |
| `SessionService` | Session 身份、配置、正式消息历史和生命周期 Hook | Inbox pending、worker 和队列协议 |

客户端只理解 `session.prompt`、`session.updateQueue`、`session.cancel`、
`session/queue`、`session/status` 和统一 ACK/error envelope；`next-turn`、
`next-step`、revision、capacity、source 不泄漏到 wire。

## 1. 历史阶段地图（非当前运行契约）

本节至第 7 节保留 A/B 阶段的演进记录。它们中的 `SessionLane`、`MailboxStore`、
`state.json.mailbox` 和 `mailbox_snapshot` 只用于解释历史决策；当前实现和新开发必须遵循
上方 F12 契约，不得从这些历史图中恢复旧 Owner。

```mermaid
flowchart LR
    A1["A1 基础通信<br/>Channel / EventBus / AgentLoop"]
    A2["A2 Session 数据<br/>state.json / messages / mailbox"]
    A3["A3 工具入口<br/>task / send_message / team"]
    A4["A4 Bus 协议<br/>Pydantic Payload / WS frame"]
    B1["B1 SessionLane<br/>FIFO / admission / cancel"]
    B2["B2 历史压缩<br/>70% / 80% / fast"]
    B3["B3 TurnExecutor<br/>单轮状态机 / Projection"]
    D1["D1 测试与 CI<br/>pytest / trace / 回归"]
    A1 --> A4
    A1 --> B1
    A2 --> B1
    A3 --> B1
    A4 --> B1
    B1 --> B2
    B1 --> B3
    B2 --> B3
    B3 --> D1
```

| 层 | 事实源 | 不应该负责的事情 |
|---|---|---|
| Channel | `PRD-A1`、`PRD-A4` | 不执行 Agent、不管理 FIFO、不猜测 Turn 状态 |
| EventBus | `PRD-A1`、`PRD-A4` | 不持久化业务消息、不作为崩溃恢复队列 |
| AgentLoop | `PRD-A1`、`PRD-B1` | 不直接实现单轮 Agent 逻辑、不复制 Session 状态 |
| SessionLane | `PRD-B1` | 不实现 LLM 压缩算法、不聚合 Reply 内容 |
| MailboxStore / state.json | `PRD-A2`、`PRD-B1` | 不保存 active Turn 或 CompletionRegistry 结果 |
| `ftre-compaction`（可选 Service + Hook） | `PRD-F11`（取代 B1/B2 历史 Owner） | 不领取队列、不直接向 WebSocket 发消息 |
| TurnExecutor / Projection | `PRD-B3` | 不决定下一条何时领取、不启动自动压缩 |
| 测试与 CI | `PRD-D1` | 不新增业务行为；只验证各阶段契约 |

## 2. 历史端到端流程（非当前运行契约）

```mermaid
flowchart LR
    CH["Channel.receive<br/>归一化 data/metadata"]
    BUS["EventBus.request_inbound<br/>一次 request/reply"]
    LOOP["AgentLoop._consume<br/>按 session 路由"]
    ADMIT["SessionLane.submit<br/>持久接纳"]
    PENDING["state.json<br/>mailbox.pending"]
    GATE1["agent/pre-step Hook<br/>领取前门控"]
    COMPACT["ftre-compaction<br/>CompactionService"]
    TAKE["MailboxStore.take<br/>pending → 内存 active"]
    TURN["TurnExecutor.execute<br/>COMMAND → BUILDING → RUNNING"]
    PROJ["SessionProjection<br/>Msg checkpoint"]
    EVENT["agent_event / mailbox_snapshot<br/>EventBus outbound"]
    DONE["TurnOutcome<br/>CompletionRegistry"]
    GATE2["agent/after-turn Hook<br/>轮后门控"]

    CH --> BUS --> LOOP --> ADMIT --> PENDING
    PENDING --> GATE1
    GATE1 -->|"需要压缩，队首不动"| COMPACT --> GATE1
    GATE1 -->|"允许领取"| TAKE --> TURN --> PROJ --> EVENT
    TURN --> DONE --> GATE2
    GATE2 -->|"需要压缩"| COMPACT
    GATE2 -->|"继续"| PENDING
    ADMIT -.->|"AdmissionResult / ACK"| CH
```

### 2.1 三种“成功”必须分开

1. **接纳成功**：`AdmissionResult.accepted=True`，表示 request 已原子写入
   `mailbox.pending`。这是 WS、HTTP、invoke、task、team_say 可以立即返回的成功。
2. **执行开始**：队首通过 `agent/pre-step` 后被 `MailboxStore.take` 领取，形成内存
   `TurnOperation`。此时它不再属于 pending，也不应再次执行。
3. **执行完成**：TurnExecutor 返回 `TurnOutcome`，Reply/消息投影已收尾，
   CompletionRegistry 唤醒同进程等待者。它不是 Bus ACK，也不是 pending 快照。

## 3. 历史数据和状态模型（非当前运行契约）

### 3.1 持久数据

- `state.json.session`：Session 身份、channel、workspace、title。
- `state.json.messages`：已投影的 User/Assistant/compact Msg；按数组顺序保存历史。
- `state.json.mailbox.pending`：尚未领取的 QueueItem；只有它决定刷新后还能执行什么。
- `state.json.mailbox.revision`：Mailbox 快照版本；运行态变化也会推进它，但不写 active 对象。

### 3.2 进程内数据

- `SessionLane._operation`：当前 Turn、压缩或 blocked 状态。
- `SessionLane._worker`：每个 session 唯一消费任务。
- `CompletionRegistry`：当前进程按 request_id 等待 TurnOutcome。
- `SessionProjection`：正在生成的 Reply 和活跃 compact 事件；语义边界 checkpoint 后才能恢复。

### 3.3 不持久化的异常语义

`MailboxStore.take` 成功后到 UserMsg 写入前进程退出，active 请求不自动重放；
这是为了避免 bash、write、MCP 等工具产生重复副作用。已 checkpoint 的 Reply Msg
仍保留，用户可以根据历史继续发起下一条请求。

## 4. 历史 ID 模型（非当前运行契约）

| ID | 生产位置 | 生命周期 | 用途 |
|---|---|---|---|
| `BusMessage.id` | EventBus 创建/接收 Bus 信封时 | 进程内一次 request/reply | `EventBus._inbound_replies` 的传输关联键 |
| `request_id` | WS `frame_id` 或服务端入口生成 | 业务请求生命周期 | Mailbox 幂等、UserMsg 关联、取消、CompletionRegistry 等待 |
| `turn_id` | SessionLane 成功 take 后生成 | 一次实际执行 | Reply/Turn 事件关联；一个 request 最多对应一个实际 Turn |

`BusMessage.id` 不能替代 `request_id`。Bus 重试可以产生新的 Bus 信封，但必须复用同一个业务 `request_id`，否则会绕过 Mailbox 幂等。

## 5. 历史 request_id 规则（非当前运行契约）

`request_id` 是一次用户输入在以下位置的同一标识：

`WS frame_id → InboundMetadata.request_id → QueueItem.request_id → UserMsg.metadata.request_id → cancel / wait`

- WS 用户消息必须有非空 frame_id；重试必须复用原 frame_id。
- 服务端内部入口没有 request_id 时，只能在最外层生成一次，后续不得层层生成新 ID。
- 相同 request_id 在 pending 或已写入 UserMsg 时只返回已有接纳/去重结果，不重复执行。
- `turn_id` 只标识一次实际执行，不替代 request_id；一个 request 最多产生一个 Turn。

## 6. 历史状态快照契约（非当前运行契约）

`session_event:mailbox_snapshot` 是队列和运行态的权威投影，Payload 包含：

`session_id、revision、phase、pending、capacity、accepting_messages、can_cancel_active、blocked_reason`

- `phase` 只表示当前 Lane operation：`idle / running / cancelling / compacting / blocked`。
- `pending` 是独立列表；`phase=idle` 不等于 pending 为空，客户端必须同时读取两者。
- attach 时先登记连接，再在同一输出锁内发送 `reply_snapshot`；之后的实时事件不能插到快照之前。
- `agent_event` 的顺序保证是 Projection 落盘成功后再广播；实时流不是历史事实源。
- `turn_cancel` 是控制面消息，不进入 mailbox，不写成 `/cancel` UserMsg。

## 7. 历史失败、取消与恢复矩阵（非当前运行契约）

| 场景 | 结果 | pending | messages | 客户端应看到 |
|---|---|---|---|---|
| 队列已满 | `accepted=False / queue_full` | 不变 | 不变 | failed ACK |
| Session 关闭/删除 | `accepted=False / session_closing` | 保留至删除或显式处置 | 已有历史保留至删除 | failed ACK |
| 取消 active | 当前 Turn `cancelled` | 不变，继续 FIFO | 已写 User/Reply 标记 interrupted | cancelling → 下一状态 |
| 取消 queued | 指定 request `cancelled` | 删除该项 | 不写历史 UserMsg | 快照移除该项 |
| 压缩失败但仍安全 | 由可选 `ftre-compaction` Hook 处理 | 队首保持 pending | 不写错误摘要 | compacting/blocked 事件 |
| 压缩后仍超硬线 | `blocked` | 队首保留 | 历史不被盲跑 | blocked_reason |
| Gateway 重启 | 恢复 pending，active 不重放 | pending 恢复 | messages/已 checkpoint Reply 恢复 | attach snapshot |

## 8. 反推 PRD 的验收方法

每次修改代码或协议，至少回答以下问题，并把结果写回对应阶段 PRD 的变更记录：

1. 输入从哪个 Channel 进入？是否经过 EventBus request/reply？
2. durable admission 的落盘点在哪里？ACK 是否早于落盘？
3. request_id、turn_id、Msg.id 各自代表什么？是否被错误复用？
4. 消息当前属于 pending、active、messages 还是 CompletionRegistry？重启后去哪儿？
5. 取消、压缩失败、队列满、Session 删除和 Gateway 停止时，谁负责收尾？
6. 客户端只订阅哪些下行事件？是否需要依赖刷新或猜测状态？
7. 是否有自动化测试覆盖正常路径、边界路径和恢复路径？未覆盖项必须标为待补。

阶段 PRD 的「验收标准」只允许勾选已经有测试或可复现实验依据的条目；
仅仅“代码看起来实现了”不能作为验收证据。

## 9. 当前待补验证项

以下不是架构设计缺口，而是当前代码尚未由独立自动化用例证明的地方，不能在阶段 PRD 中继续无条件标记为“已验证”：

- `send_message` 的 notify/invoke 路由、队列满和自发消息拒绝。
- team 工具的多条成员消息、request_id 等待和 team_delete 竞态。
- `wait_session_quiescent` 与 Turn 收尾之间的瞬时竞态，需要用测试证明不会提前返回。
- Gateway stop 与 WS ingress、真实 compact Task 的完整顺序，需要集成测试覆盖。
- 全量 `pytest -q`、ruff 和干净环境 CI 结果，需要在 D1 留存命令输出。

## 10. 文档变更规则

- 同阶段范围内的职责澄清、字段修正、验收细化：修改原 PRD 正文，并在末尾追加日期、内容、理由和受影响 AC 复核结果。
- 新增跨阶段协议或改变持久化语义：先修改本文，再同步 A4/B1/A2 等事实源；如果超出现有阶段范围，新增 TODO 阶段 PRD。
- 不把临时兼容字段写成长期契约；协议字段必须说明生产方、消费方、持久化与生命周期。

## 11. Command Plane 与 Agent Plane 边界（F8）

F8 将 Command 与 Agent 明确分成两条执行平面。此处是跨阶段架构契约，
具体迁移任务和验收条目见 [PRD-F8](./PRD-F8-command-plane-agent-plane.md)。

```text
Channel / EventBus
        │
        ▼
CommandIngress → CommandRuntime → Domain Service
        │                  │
        │                  ├─ command/run + command/done
        │                  ├─ CommandResult → 客户端
        │                  └─ Domain Event
        │
        └─ 已有 Session Event → 现有 Agent/Session 恢复流程
```

不可违反的规则：

- `CommandResult` 只表示 Command 成功或失败，不是 Agent 输入；
- 纯 Command 不创建 Turn、不进入 Mailbox、不产生 LLM request；
- `/allow`、`/deny` 等需要恢复 Agent 的命令复用已有 Session Event，不新增
  `AgentEffect`、`AgentControlPort` 或 `AgentResumeRequest`；
- Agent 原始输入必须经过 `ftre-inbox` 的候选 `→ Hook → claim`，再进入 Agent Hook 和
  `TurnExecutor`；
- `TurnExecutor` 不依赖 Command 类型，也不负责解析或解释 Command；
- `persist_input` 只描述命令审计/历史策略，不能作为创建 Agent Turn 的隐式开关；
- Command Handler 直接依赖公开 `CompactionService`、`SessionService` 等真实 Service Owner，
  不得通过 `AgentLoop.compaction` 或 `AgentLoop.session_manager` 间接取 Service；
- `command_id`、`request_id`、`turn_id` 各自独立，不能相互替代。

## 12. Service 依赖注入与架构债务边界（F9）

F9 负责清理 Service 之间通过 Loop 字段、手工构造、动态查找和具体实现 import
形成的隐藏耦合。详细任务和验收见 [PRD-F9](./PRD-F9-service-injection-and-debt-cleanup.md)。

```text
Plugin
  → inject 公开 Service key
  → ctx.<service_key>
  → 创建自己的实现
  → provide 自己的 Service key

Service
  → 只持有由 Plugin/Provider 注入的公开依赖
  → 不从 AgentLoop、全局单例或 Context 深处反查 Service
```

不可违反的规则：

- `AgentLoop` 是数据面运行时，不是 Service Locator；禁止通过
  `loop.session_manager`、`loop.compaction` 等字段获取公共 Service；
- Service 的唯一实现由自己的 Plugin、Composition Root 或明确 Provider 创建；
- 必选依赖必须在 `inject` 或类型化 Provider 参数中声明，不能用宽松 `ctx.get` 静默替代；
- Feature/Service 跨能力协作使用 Service key、公开 Contract 或 Hook/Event，不 import
  其他 Feature 私有实现；
- `AgentLoopProvider` 可以装配内部 runtime，但业务 Handler 不得反向依赖 Loop；
- unload/restart 后注入的 Listener、Task、Router、闭包和旧 Service 实例必须全部可逆清理。

## 13. Compaction Service Owner 收敛（F10/F11）

F10 的历史迁移先将实现收敛到一个 `CompactionService`；F11 又把该唯一 Owner、
Hook、命令和算法移动到可选发行物 `packages/ftre-compaction`。当前关系是：

```text
ftre-compaction/plugin.py
  → 创建 CompactionService
  → provide("compaction")
  → 注册 inbox/before-claim、agent/after-turn、agent/request-error
  → 注册 /compact、/compress-fast
```

Agent 的两个相邻边界必须区分：`agent/before-turn` 只负责一次
`InboundMessage` 的 Turn 准入；`agent/before-reasoning` 由 `ftre-agent-core` 在每次
真正调用 LLM 前触发，`ftre-inbox` 可在这里消费运行中的 `next-step`。

不可违反的规则：

- `ftre-compaction.CompactionService` 是唯一真实压缩状态和算法 Owner；
- ftre 核心不 import、创建或要求 `compaction` Service；
- `ftre-inbox` 只提供候选 `→ Hook → claim`、队列状态和生命周期边界；
- 不新增第二个 Port、Facade、No-op fallback 或兼容别名；
- `compaction` Service key、压缩事件、命令和客户端协议保持不变；
- 未安装/未启用 `ftre-compaction` 时核心 Gateway 和普通 Agent 流程正常运行。

F10 中关于 `src/ftre/services/compaction`、`src/ftre/plugins/builtin/compaction` 和
`ContextGate` 的路径描述是历史记录；当前实现以
[`PRD-F11-compaction-gate-hook.md`](PRD-F11-compaction-gate-hook.md) 为准。
