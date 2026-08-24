# PRD-F12 独立 Inbox Package 与权威队列协议

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F12 |
| 名称 | 独立 Inbox Package 与权威队列协议 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-23 |
| 定稿日期 | 2026-08-23 |
| 验收日期 | 2026-08-23 |
| 关联文档 | `docs/TODO.yaml` 阶段 F12；`PRD-F6-semantic-hook-system.md`；`PRD-F8-command-plane-agent-plane.md`；`PRD-F11-compaction-gate-hook.md`；`docs/prd/README.md`；`AGENTS.md`；`docs/PROCESS.md` |
| 参考实现 | `E:\deepseek-harness` 的 Agent Inbox、`session.prompt`、`session.updateQueue`、`session/queue` |

## 1. 背景与问题

### 1.1 重构前实现（已退役）

F12 重构前 FTRE 使用一条持久化 `mailbox.pending` FIFO。客户端通过 `user_message` 和
`frame_id` 提交输入，SessionLane 逐条执行：

```text
Channel
  -> EventBus
  -> SessionLane.submit
  -> mailbox.pending
  -> peek
  -> agent/before-turn
  -> take
  -> TurnExecutor
  -> session_event:mailbox_snapshot
```

该实现已经具备以下正确性基础：

- 同一 Session 同时最多一个 active Turn；
- 不同 Session 可以并行；
- admission 在 ACK 前持久化；
- `peek -> Hook -> claim` 保证 Hook 失败时 pending 不丢失；
- claim 采用 at-most-once，避免工具副作用被自动重放；
- 队列容量、取消、Gateway 重启恢复和完整快照已存在；
- Command 已与 Agent Plane 分离，不经过 TurnExecutor。

但单 FIFO 只能表达“以后执行一轮”，不能表达下面三种不同意图：

1. 用户提交一个普通后续任务；
2. 用户在 Agent 运行过程中纠正或补充当前任务；
3. Plugin 为当前/最近一次 Agent Step 静默注入上下文。

当前协议还把 `revision`、运行 `phase`、队列容量、pending 和兼容字段集中在
`mailbox_snapshot` 中，客户端需要维护本地乐观项并与 ACK、快照反复对账。内部
`MailboxStore` 又只是对 SessionService 的薄转发，队列的公开能力、持久化 Owner 和
AgentRuntime 编排边界没有形成清晰契约。更关键的是，AgentService 当前仍暴露 submit、
cancel queued、mailbox snapshot 等队列操作，使“执行一条 Agent 输入”和“如何排队、何时
交付”没有真正解耦。

### 1.2 DSH 提供的参考

DeepSeek Harness 使用两个逻辑 Inbox：

```text
next-turn：每次领取一条，用于开启后续 Turn
next-step：在最近的 Agent Step 边界批量领取
```

它只提供三种写入语义：

```text
followup = next-turn + wake
steer    = next-step + wake
inject   = next-step + no wake
```

客户端只调用 `session.prompt`、`session.updateQueue` 和 `session.cancel`；服务端用
`session/queue` 推送完整权威队列。客户端不保存另一份服务端队列状态机。

FTRE 采用这组业务语义，但不照抄 DSH 的全部实现：

- 保留 FTRE 的有界队列；
- 保留 FTRE 的 snapshot 持久化，不在本阶段把整个 Session 改成事件溯源；
- 保留 `peek -> Hook -> claim`，不采用 DSH 的 claim 后再执行 pre-step；
- 保留 at-most-once，不自动重放已经 claim 的工具型 Turn；
- 内部存储字段不直接暴露给客户端。

## 2. 目标与非目标

### 2.1 目标

本阶段完成后，整个消息队列能力应迁入可独立构建、安装、启用和发布的
`packages/ftre-inbox`。该 Package 唯一拥有 `next-turn` / `next-step`、持久化、worker、
客户端队列协议以及 `followup` / `steer` / `inject`。FTRE 内置 AgentService 只负责执行
已经交付的 `InboundMessage`，不知道 QueueItem、pending、capacity、placement 或队列快照。

### 2.2 非目标

- 不要求基础 AgentService 依赖或 import `ftre-inbox`；
- 不把 Inbox Package 的模型复制一份到 `src/ftre/`；
- 不让 Compaction、Plan、Team 或 Channel 成为 pending 数据 Owner；
- 不引入 `AgentControlPort`、`AgentEffect`、`QueueCommand` 等额外控制抽象；
- 不为每条 QueueItem 建立 `queued/running/completed/failed` 状态机；
- 不把已执行消息、active Turn 或同进程 Completion 结果写进 Inbox；
- 不在本阶段把 Session 持久化整体迁移为 DSH 的 splice 事件溯源；
- 不允许浏览器/桌面端直接调用 `inject`；
- 不把同步 ToolResult 重新放入 Inbox；
- 不恢复 Command 进入 Agent 消息队列的旧路径；
- 本仓库不直接修改 `E:\binn\ftre-desktop`，但 Inbox Package 必须冻结并测试其可消费的
  新版线协议。

## 3. 术语与所有权

### 3.1 术语

| 术语 | 含义 |
|---|---|
| Inbox | 某个 Session 尚未被 Agent 消费的持久化输入集合 |
| `next-turn` | 等待开启新 Turn 的队列；每个新 Turn 最多领取一条 |
| `next-step` | 等待进入当前或最近 Agent Step 的队列；一个边界可以批量领取 |
| `followup` | 写入 `next-turn` 并唤醒 Inbox worker |
| `steer` | 写入 `next-step` 并唤醒/通知 Inbox worker |
| `inject` | 写入 `next-step`，但不主动唤醒空闲 Agent |
| placement | 客户端展示语义：`queued` / `steering` / `context` |
| claim | 按 ID 和快照条件原子删除 pending，并将消息交给当前运行单元 |

### 3.2 唯一 Owner

```text
[独立 Package] ftre-inbox
  - QueueItem / InboxState
  - pending 持久化和恢复
  - next-turn / next-step
  - capacity / idempotency / claim
  - followup / steer / inject
  - 每 Session Inbox worker
  - session.prompt / updateQueue / cancel
  - session/queue 权威投影
  - 调用 AgentService.run(InboundMessage)

[内置] AgentService
  - run(InboundMessage) -> TurnOutcome
  - active Turn guard / cancel / when_idle
  - Session 对应 Agent 的选择
  - Agent Hook 与 Agent Core 调用
  - 不知道 QueueItem、pending、target、capacity 和 queue snapshot

[内置] SessionService
  - Session 身份与配置
  - 已提交的正式消息历史
  - 不公开 mailbox 操作

[可选] 其他 Feature/Package
  - 通过 Inject 使用公开 InboxService（存在时）
  - 或响应 Agent/Inbox Hook
  - 不访问 Inbox 私有存储
```

依赖方向必须单向：

```text
ftre-inbox -> ftre 的稳定 Plugin/Hook/Agent/Session 契约
ftre       -X-> ftre-inbox
```

基础 ftre 未安装或未启用 `ftre-inbox` 时，AgentService 仍可被内部调用方直接传入
`InboundMessage` 执行。只有 `session.prompt` 排队能力不可用，并返回明确的 capability
unavailable，而不是导致 Gateway 或 AgentService 启动失败。

### 3.3 Hook 边界

Hook 插槽由内核定义，处理器由 Plugin 注册：

```text
ftre-inbox 候选输入
  -> [Package Hook 点] inbox/before-claim
       -> [Plugin] Compaction
       -> [Plugin] Inbox policy
  -> 原子 claim
  -> AgentService.run(InboundMessage)
       -> [ftre Hook 点] agent/before-turn（一次 Turn 准入）
       -> [Core Hook 点] agent/before-reasoning（每次 LLM 前）
            -> [ftre-inbox Plugin] 原子领取并提供 next-step 输入
            -> [其他 Plugin] 提供非队列上下文
       -> [Plugin] system-prompt / llm / tool / turn-stopping
  -> [内置 Hook 点] agent/after-turn
       -> [Plugin] Compaction / Summary
```

AgentService 只认识 `InboundMessage` 和通用 Agent Hook。它不会调用 `peek()`、`claim()`
或判断 next-step。相关操作全部封装在 ftre-inbox 注册的 Hook listener 内。Inbox 的
admission、持久化、排序和 claim 也不是可被其他 Feature 替换的 Hook。

## 4. 功能需求

### 4.1 Inbox Service 与数据模型

- [x] **FR1：独立可发布的 ftre-inbox Package**
  - 新增 `packages/ftre-inbox`，拥有自己的 `pyproject.toml`、源码和测试；
  - Package 提供稳定 Service key `inbox` 和 Cordis Plugin 入口；
  - ftre-inbox 可以独立 build、wheel 安装、uninstall 和洁净环境 import；
  - ftre 核心源码不得 import `ftre_inbox`，基础 AgentService 不以它为启动依赖；
  - 不提供 No-op Inbox、全局 setter 或从 AgentLoop 字段反查 Inbox 的兼容路径。

- [x] **FR2：双队列持久化状态**
  - 每个 Session 的 Inbox 至少持久化 `next_turn`、`next_step`、`next_sequence` 和
    内部 `revision`；
  - 两条队列共享一个容量上限，默认延续现有 mailbox capacity；
  - 同一个消息 ID 在两条队列中全局唯一；
  - Session fork 默认不继承 Inbox；
  - Gateway 恢复时同时恢复两条队列，但不恢复 active Turn。

- [x] **FR3：保持 QueueItem 最小化**
  - QueueItem 只保存稳定 ID、sequence、source、content、attachments、created_at 等
    pending 必需信息；
  - `agent_id` 属于 Session/AgentService 选择，不作为每条消息可任意切换的字段；
  - `target` 由 QueueItem 所在列表表达，不在对象内重复保存；
  - QueueItem 不保存 running/completed/failed 等执行状态。

- [x] **FR4：三种写入语义**
  - `followup(session_id, message)` 原子写入 `next-turn` 并请求唤醒 Inbox worker；
  - `steer(session_id, message)` 原子写入 `next-step` 并请求唤醒/通知 Inbox worker；
  - `inject(session_id, message)` 原子写入 `next-step`，但空闲时不创建 Turn；
  - 三种入口复用同一 admission、幂等、容量和持久化实现；
  - Plugin 不需要构造 BusMessage 才能注入上下文。

- [x] **FR5：原子队列操作**
  - 支持按 ID `edit`、`remove` 和把 `next-turn` 项提升为 `next-step` 的 `steer`；
  - 操作只对仍处于 pending 的项目成功；
  - 已 claim、已删除或未知 ID 返回稳定业务错误，不修改其他项目；
  - edit 不改变消息 ID 和原始先后位置；
  - 所有成功 mutation 都持久化后再发布权威快照。

### 4.2 消费、并发与 Hook

- [x] **FR6：AgentService 完全移除队列概念**
  - AgentService 的主执行入口接收一个 `InboundMessage`，返回既有 TurnOutcome/事件流；
  - AgentService 不提供 submit、cancel_queued_message、get_mailbox_snapshot、queue depth
    或 queue capacity；
  - AgentService 可以保留 active Turn guard、cancel active 和 when_idle，它们是运行互斥，
    不是 pending 队列；
  - 直接调用 AgentService 时，busy 由稳定错误表达，不在内置 Service 中偷偷排队。

- [x] **FR7：保留 peek -> Hook -> claim 不变量**
  - 候选消息在领取前保持 pending；
  - Hook 返回 enter 后，按候选 ID 原子 claim；
  - Hook 返回 keep 或执行失败时消息保持 pending；
  - Hook 明确返回 discard 时才删除候选并发布丢弃观察事件；
  - claim 与客户端 edit/remove 竞争时只允许一个操作成功。

- [x] **FR8：Turn 与 Step 消费规则**
  - 新 Turn 的首次消费边界读取全部可消费 `next-step`，并最多读取一条
    `next-turn`；
  - active Turn 后续安全边界只读取 `next-step`，不得提前领取下一条
    `next-turn`；
  - 一批消息的 Hook 决策和 claim 必须保持一致，不能只领取半批后静默继续；
  - 同步 ToolResult 属于当前 Turn，不进入 Inbox；
  - 异步 Plugin 结果根据意图调用 `steer`、`inject` 或 `followup`。
  - `agent/before-turn` 只在新 `InboundMessage` 进入 active Turn 前触发一次；
  - `agent/before-reasoning` 由 Core 在首次 Reasoning、Tool 后和 continuation 后的每次
    LLM 调用前触发；它返回的消息在 Core 内进入本次 LLM snapshot；
  - ftre-inbox 只在 `agent/before-reasoning` 中消费 `next-step`，不把队列模型传入 AgentService。

- [x] **FR9：Session 与 Agent 选择**
  - 不同 Session 可以使用不同 Agent；
  - 同一 Session 的 Agent 选择由 Session 配置/AgentService 管理；
  - 客户端 QueueItem 不携带任意 `agent_id` 来绕过 Session 选择；
  - Agent 配置变更与 pending 的解释规则必须在实现前由配置契约测试固定。

- [x] **FR10：取消与唤醒**
  - `session.cancel` 取消当前 active Turn，默认保留两条队列；
  - remove 只取消指定 pending 项，不影响 active Turn；
  - followup/steer 必须触发 wake；inject 不触发 idle wake；
  - close/unload 必须取消 worker 和 in-flight Hook，并保留已经持久化但尚未 claim 的项；
  - 同 Session 仍最多一个 active Turn，不同 Session 保持并行。

### 4.3 客户端协议

- [x] **FR11：`session.prompt` 写协议**
  - 客户端通过 `session.prompt` 提交内容；
  - payload 只包含 `session_id`、`mode: queue | steer`、结构化 content 和必要附件；
  - request correlation ID 位于统一传输信封，不在业务 payload 重复保存；
  - 客户端不能提交 `mode=inject`；
  - 服务端只在持久化 admission 成功后返回 `{accepted: true}`。

- [x] **FR12：`session.updateQueue` 与 `session.cancel`**
  - `session.updateQueue` 只支持 `edit`、`remove`、`steer`；
  - `session.cancel` 不要求客户端重复传 `keep_queue=true`；
  - 错误使用稳定 code，例如 `queue-full`、`item-not-pending`、
    `steer-not-available`、`session-not-found`；
  - 业务错误走统一 RPC/WS error envelope，不伪装成队列快照。

- [x] **FR13：`session/queue` 权威快照**
  - 服务端在每次成功 mutation 和客户端 attach/reconnect 后推送完整队列；
  - wire item 只暴露 `id`、`placement` 和可展示的 message；
  - placement 为 `queued`、`steering` 或 `context`；
  - 内部 `revision`、`next-turn`、`next-step`、source、capacity 不作为客户端必须理解的
    状态机字段；
  - 客户端 QueueMirror 使用完整快照替换，不根据 ACK 猜测服务端位置。

- [x] **FR14：状态与事件分离**
  - `session/queue` 只表达 pending；
  - active/idle/maintenance/blocked 由独立 `session/status` 表达；
  - Reply、Tool 和持久消息继续使用 `session/event`；
  - ACK、queue、status、event 各自只有一个语义，不互相嵌套复制。

- [x] **FR15：Command 保持旁路**
  - Slash Command 在接入/输入裁决层交给 CommandService；
  - Command 不写入 `next-turn` 或 `next-step`；
  - CommandResult 不作为 Agent 输入 ACK；
  - 命令需要排队时，只能在 Inbox Package 已启用时 Inject 公开 InboxService；否则复用
    已有 Session Event 恢复机制，不允许 TurnExecutor 解释命令。

### 4.4 清理与迁移

- [x] **FR16：旧 Mailbox API 与薄壳清理**
  - 删除 `src/ftre/services/agent_loop/runtime/mailbox` 中的 Store/Lane 队列实现；
  - SessionLane 的 pending/worker 职责迁入 ftre-inbox；AgentService 只保留 active 执行边界；
  - SessionService 删除被 ftre-inbox 接管的公开 mailbox 操作；
  - `mailbox.pending` 单队列只允许作为一次性数据迁移输入，不保留运行时双写；
  - 删除 `mailbox_snapshot`、`queue_position`、`frame_id` 输出别名和客户端乐观队列所需的
    长期兼容分支；
  - 清理旧类型、测试替身、文档、空目录和缓存；
  - 架构测试禁止 Feature 私自持有 pending 列表或 import Inbox 私有存储。

- [x] **FR17：现有数据迁移**
  - 旧 `mailbox.pending` 项按原 sequence 全部迁入 `next-turn`；
  - 迁移幂等，重复启动不得复制消息；
  - 不可解析的数据产生可定位诊断，不静默清空整个 Session；
  - 首次成功保存新格式后不再写旧格式；
  - 迁移测试覆盖空队列、多项队列、附件、重复 request_id 和损坏输入。

## 5. 数据模型草案

### 5.1 内部持久化模型

```text
InboxState
├─ revision: int
├─ next_sequence: int
├─ next_turn: list[QueueItem]
└─ next_step: list[QueueItem]

QueueItem
├─ id: str
├─ sequence: int
├─ source: user | plugin | system
├─ content: structured content
├─ attachments: list[AttachmentRef]
└─ created_at: datetime
```

`revision` 只用于服务端 mutation、测试和诊断。客户端收到的是完整快照，不需要参与
乐观并发控制。

### 5.2 数据归属

```text
InboxService.pending     -> 尚未 claim，可恢复
ftre-inbox worker        -> pending 调度、wake、claim 和重启恢复
AgentService.active      -> 已交付 InboundMessage，进程内，at-most-once
SessionService.messages  -> 已提交的正式历史，可恢复
内部 wait receipt        -> 仅同进程 task/team 等调用方，可选，不进入 wire
```

不再使用一个客户端可见的 `CompletionRegistry` 来表达“这一条 Prompt 的最终结果”。
如果内部 task/team 确实需要精确等待，保留一个进程内 receipt 即可，其生命周期不能反向
决定 Inbox 协议。

## 6. 目标流程

### 6.1 外部输入

```text
Desktop / HTTP / WS
        |
        | session.prompt(mode=queue|steer)
        v
Channel / RPC 边界（由 ftre-inbox 贡献 session.prompt）
        |
        +-- slash command --> CommandService --> CommandResult
        |
        `-- agent prompt ---> InboxService
                                  |
                                  +-- queue --> next-turn + wake
                                  `-- steer --> next-step + wake
```

### 6.2 Plugin 输入

```text
Schedule Plugin -------- followup ------> next-turn + wake
Team Plugin ------------ steer ---------> next-step + wake
Plan/Context Plugin ---- inject --------> next-step
```

### 6.3 Agent 消费

```text
ftre-inbox worker 到达安全消费边界
        |
        | peek candidate batch
        v
领取前 Hook 管线
        |
        +-- keep ----> pending 不变，进入 blocked/等待恢复
        +-- discard -> 原子删除，发布观察事件
        `-- enter ---> 原子 claim exact IDs
                              |
                              v
                  AgentService.run(InboundMessage)
                              |
                         Agent Core
                              |
                              +-- 同步 ToolResult 留在当前 Turn
                              |
                              `-- 下一安全 Step 再检查 next-step
```

## 7. 线协议草案

### 7.1 统一请求信封

```json
{
  "type": "session.prompt",
  "request_id": "uuid",
  "payload": {
    "session_id": "session-id",
    "mode": "queue",
    "content": [
      {"type": "text", "text": "你好"}
    ]
  }
}
```

成功响应：

```json
{
  "request_id": "uuid",
  "ok": true,
  "value": {"accepted": true}
}
```

### 7.2 队列操作

```json
{
  "type": "session.updateQueue",
  "request_id": "uuid",
  "payload": {
    "session_id": "session-id",
    "item_id": "message-id",
    "action": {"kind": "remove"}
  }
}
```

`edit`：

```json
{
  "kind": "edit",
  "content": [
    {"type": "text", "text": "修改后的内容"}
  ]
}
```

`steer`：

```json
{"kind": "steer"}
```

### 7.3 权威快照

```json
{
  "type": "session/queue",
  "session_id": "session-id",
  "items": [
    {
      "id": "message-id",
      "placement": "queued",
      "message": {
        "content": [
          {"type": "text", "text": "下一条消息"}
        ]
      }
    }
  ]
}
```

内部映射：

```text
next-turn                         -> queued
next-step + source=user           -> steering
next-step + source=plugin/system  -> context
```

### 7.4 状态和事件

```text
session/queue   -> pending 的完整瞬时投影
session/status  -> idle/running/maintenance/blocked
session/event   -> User/Reply/Tool/Command 等持久或流式事件
```

## 8. 失败、并发与恢复语义

| 场景 | 结果 | Inbox | 客户端 |
|---|---|---|---|
| 队列已满 | admission 失败 | 不变 | `queue-full` |
| 重复 request ID | 返回原接纳结果 | 不重复插入 | accepted 或稳定冲突错误 |
| pre-claim Hook 失败 | 阻止 claim | 候选保留 | status=blocked |
| edit 与 claim 竞争 | 仅一方成功 | 原子一致 | loser 收到 `item-not-pending` |
| steer 时 Agent idle | 写 next-step 并 wake | 可进入最近新 Turn | 新 queue snapshot |
| inject 时 Agent idle | 只写 next-step | 不启动 Turn | context queue item |
| cancel active | 当前 Turn 取消 | pending 全保留 | status 更新 |
| Gateway 重启 | active 不重放 | 两队列恢复 | attach 完整 snapshot |
| Plugin unload | Listener/Task 清理 | Inbox 不受影响 | 队列继续可用 |

## 9. 实施切片

### F12.1 语义与协议冻结

- 完成 DSH 与 FTRE 行为对照；
- 冻结 Inbox Owner、双队列、三种写入语义和 wire vocabulary；
- 解决第 12 节中的 Step 边界评审问题；
- PRD 从草稿进入 approved 后才开始代码迁移。

### F12.2 独立 Package 与持久化迁移

- 建立 `packages/ftre-inbox`、`inbox` Service/Provider 和独立构建配置；
- 迁移 Session mailbox 数据和 repository mutation；
- 建立旧单队列到 `next-turn` 的一次性迁移；
- 删除 SessionService 与 MailboxStore 的重复 Owner。

### F12.3 双队列原子操作

- 实现 followup/steer/inject；
- 实现 batch peek/claim、edit/remove/promote；
- 固定容量、幂等、sequence 和竞争语义；
- 发布 mutation 观察事件。

### F12.4 AgentService 瘦身与 Hook 交付

- ftre-inbox worker 在新 Turn 领取 next-step batch + 一条 next-turn；
- AgentService 只接收 InboundMessage，删除全部 queue API 和模型依赖；
- ftre-inbox 通过通用 Agent Step Hook 在 active Turn 安全边界贡献 next-step；
- 保留 Inbox before-claim Hook 和 Agent after-turn barrier；
- 保持同 Session 串行、跨 Session 并行和 at-most-once。

### F12.5 Session 协议与 Gateway 接入

- 实现 `session.prompt`、`session.updateQueue`、`session.cancel`；
- 实现 `session/queue` 和独立 `session/status`；
- attach/reconnect 推送完整基线；
- Command 保持旁路。

### F12.6 客户端契约与旧协议清理

- 在本仓库提供 wire schema、协议测试和客户端迁移说明；
- 删除旧 `user_message` / `mailbox_snapshot` 长期兼容路径；
- 删除乐观 queue position 和 frame_id 输出别名；
- 不直接修改 Desktop 仓库。

### F12.7 并发、恢复与生命周期验证

- 覆盖两队列顺序、批量 claim、编辑竞争、取消、重启和容量；
- 覆盖 Hook keep/discard/failure 和 Plugin unload；
- 覆盖 WS attach、断线重连、快照顺序和错误 envelope；
- 运行全量质量门禁与 Gateway smoke。

### F12.8 清理与收尾

- 扫描旧 mailbox 类型、兼容分支、死代码、空目录和生成缓存；
- 更新 PRD 总览、TODO、CHANGELOG 和执行报告；
- 所有 AC 有证据后才能标记已验收。

## 10. 验收标准

- [x] **AC1**：`packages/ftre-inbox` 可独立 build、wheel 安装和洁净环境 import；基础 ftre
  未安装/未启用它时 AgentService 和 Gateway 可以启动，`session.prompt` 明确报告能力不可用。
- [x] **AC2**：followup、steer、inject 分别满足目标队列和 wake 语义，重复 ID 不会重复
  插入。
- [x] **AC3**：新 Turn 的首次候选批次为全部可消费 next-step 加最多一条 next-turn；
  active Turn 不会提前消费第二条 next-turn。
- [x] **AC4**：Hook keep、Hook failure、cancel-before-claim 均保留 pending；discard 只删除
  指定候选。
- [x] **AC5**：claim/edit/remove/steer 竞争测试重复运行无丢失、无重复消费、无半批提交。
- [x] **AC6**：旧单 pending 数据升级后顺序不变地进入 next-turn，重复恢复不复制消息，
  fork 不继承 Inbox。
- [x] **AC7**：`session.prompt(mode=queue|steer)` 只在持久化成功后返回 accepted；非法 mode
  和 queue-full 返回稳定错误。
- [x] **AC8**：`session.updateQueue` 的 edit/remove/steer 只作用于 pending 项，随后广播完整
  `session/queue`。
- [x] **AC9**：客户端协议 fixture 只依赖 placement，不读取 next-turn/next-step、revision、
  capacity 或 source。
- [x] **AC10**：attach/reconnect 后第一份 queue baseline 能完整重建 pending；后续 mutation
  快照不会被旧 baseline 覆盖。
- [x] **AC11**：`session.cancel` 取消 active 但保留两条队列；Gateway 重启恢复 pending 但不
  重放 active。
- [x] **AC12**：同步 ToolResult 不进入 Inbox；异步 Plugin 可以通过公开 InboxService 选择
  followup/steer/inject。
- [x] **AC13**：Command 不进入 Inbox、不创建 Turn；TurnExecutor 不依赖 Command 输入或
  Queue update 类型。
- [x] **AC14**：卸载 ftre-inbox 后不残留 worker、route、listener 或 pending 内存引用；直接
  `AgentService.run(InboundMessage)` 仍可执行。卸载 Compaction/Plan/Team 后 Inbox 继续工作。
- [x] **AC15**：架构测试证明 ftre 核心不 import `ftre_inbox`，AgentService 不出现 QueueItem、
  pending、capacity、placement、mailbox snapshot；Feature 不建立第二 pending Owner。
- [x] **AC16**：旧 MailboxStore 薄壳、单队列运行路径、旧快照 payload、兼容字段和陈旧测试
  替身清理完成，`rg` 审计无运行时引用。
- [x] **AC17**：`python -m pytest -q`、`python -m ruff check src tests`、
  `git diff --check` 全部通过。
- [x] **AC18**：Gateway 启停、真实 WebSocket endpoint 的 prompt/steer、queue
  edit/remove/cancel 和 reconnect smoke 全部通过；未修改桌面客户端。

> 当前状态说明：AC1-AC18 的后端证据已写入执行报告；WebSocket smoke 使用真实 FastAPI
> WebSocket endpoint，不依赖或修改桌面客户端。桌面端后续只需按已冻结 wire contract 联调。

## 11. 测试计划

### 11.1 Inbox 单元测试

- followup/steer/inject 路由和 wake 次数；
- 两队列 ordering、共享 capacity 和 duplicate ID；
- batch peek/claim 的原子性；
- edit/remove/promote 的合法和竞争路径；
- 旧 state.json 迁移、损坏输入和重复恢复。

### 11.2 AgentService 与 Inbox worker 测试

- 新 Turn 与 active Step 的不同领取规则；
- 同 Session 串行和不同 Session 并行；
- keep/discard/Hook error/cancel 的 pending 结果；
- Tool 执行期间到达 steer；
- inject 不唤醒、后续自然消费；
- close/unload 期间的 in-flight Hook 和 claim 竞争。

### 11.3 协议测试

- request envelope 与业务 schema 校验；
- prompt accepted/error；
- updateQueue 三种动作；
- queue/status/event 分流；
- attach/reconnect baseline；
- 客户端 QueueMirror 用完整快照替换；
- 旧协议删除后的明确拒绝行为。

### 11.4 架构与回归测试

- Service key 唯一 Owner 和 Inject/Provide；
- Feature 私有 import 门禁；
- Command Plane 边界；
- Compaction package 启用/禁用；
- Session fork、search、attachments、Schedule、Team 和 Subagent 回归；
- 全量 pytest、ruff、diff check 和 Gateway smoke。

## 12. 评审前必须冻结的问题

### 12.1 运行中 steer 的真实 Step 边界（已冻结）

历史 FTRE 的 SessionLane 只能看到 Turn 边界；`ftre-agent-core` 内部才拥有每次
Reasoning/Tool 后的 ReAct Step 边界。要实现 DSH 的“active Turn 下一 Step 消费”，不能
假装 SessionLane 已经能在 Core 内部插入消息。

冻结方案 1：`E:\ftre-agent-core` 提供最小通用 `agent/before-reasoning` Hook，ftre-inbox
在每次新 LLM Reasoning 前原子领取并贡献 `next-step` 消息。Agent Core 和 AgentService
都不 import 或调用 InboxService；Core 只接收通用 message mapping。

本方案已由 F12/C2 跨仓库阶段授权，Core 和 ftre 分别在独立 feature 分支实现和验证。
不采用把 steer 塞进 `agent/turn-stopping` 的方案，避免把用户纠正消息绑定到
continuation 次数限制。

### 12.2 Hook 命名（已冻结）

不再使用含义模糊的 `agent/pre-step`。四个边界的名称和 Owner 固定为：

| Hook | Owner | 触发时机 |
|---|---|---|
| `inbox/before-claim` | ftre-inbox | pending 原子 claim 前的队列策略 |
| `agent/before-turn` | ftre AgentLoop | 一条 InboundMessage 开始一个 Turn 前 |
| `agent/before-reasoning` | ftre-agent-core | 每次真正 LLM Reasoning 前 |
| `agent/after-turn` | ftre AgentLoop | Turn 完成或取消后的维护屏障 |

不得让不同 payload、不同触发时机的 Hook 共用一个名字。

## 13. 变更记录

| 日期 | 变更内容 | 理由 | 受影响验收 |
|---|---|---|---|
| 2026-08-23 | 初始草案：确定 Inbox 为内置必选 Service；引入 next-turn/next-step、followup/steer/inject 和 DSH 风格 Session 协议；保留 FTRE peek→Hook→claim、有界队列、snapshot 恢复与 at-most-once | 单 FIFO 无法区分后续 Turn、运行中 steer 和 Plugin context；现有 mailbox/客户端协议暴露过多内部状态 | 新增 AC1-AC18，尚未执行 |
| 2026-08-23 | 按架构评审修正 Owner：整个队列模型、持久化、worker 和客户端 queue 协议迁入独立 `ftre-inbox` Package；AgentService 只执行 InboundMessage，并通过通用 Agent Hook 接收 Package 贡献的 Step 输入 | 让 AgentService 完全不知道队列，同时允许队列能力独立安装、卸载和发布；避免“内置 InboxService”继续把队列生命周期绑在 Agent 内核 | 重写 FR1、FR6-F17、AC1、AC14-AC15；其余 AC 待实现后统一执行 |
| 2026-08-23 | 实施迁移：新增独立双队列 Package、旧 `mailbox.pending` 一次性迁移、AgentService.run(InboundMessage)、现代 Prompt/Queue/Status wire、Command 旁路、Hook/生命周期/架构测试；删除核心 SessionLane、MailboxStore、旧 mailbox payload 和 queue position/frame_id 兼容路径 | 让队列持久化和调度真正脱离 ftre Agent 数据面，同时保留 at-most-once、重启恢复和权威快照 | AC1-AC16 已有专项证据；AC17-AC18 待最终门禁与 Gateway smoke |
| 2026-08-23 | 冻结 Hook 命名与 Step 边界：`agent/before-turn` 负责 Turn 准入，Core `agent/before-reasoning` 负责每次 LLM 前注入，Inbox `inbox/before-claim` 负责队列 claim；授权独立 Core C2 并接入 ftre-inbox | 消除 `agent/pre-step` 与真实 ReAct Step 混淆，使运行中 steer 能在 active Turn 内被消费且不把 QueueItem 传入 AgentService | FR8、F12.4、C2 AC1-AC7 |
| 2026-08-23 | ftre 将 `ftre-agent-core` 最低依赖提升到 `>=0.1.2`，对应 C2 公共 Hook；Core wheel/PyPI 发布保持独立安排，不在本阶段代发 | 新增 Hook 是跨仓库公开契约，不能让运行时静默依赖旧 Core | F12.10、C2 |
| 2026-08-23 | 完成 AC18 后端 smoke：实际启动/取消 Gateway runtime，并通过 FastAPI WebSocket endpoint 验证 attach、queue/steer prompt、edit、remove、cancel、reconnect；新增 `tests/startup/test_f12_ws_smoke.py` | 在不修改桌面客户端的边界内验证完整后端 wire/lifecycle 链路 | AC18、F12.9 |
| 2026-08-23 | 收尾复审：重复/已完成 `next-turn` 不再创建无法完成的 receipt；AgentLoop 关闭时清理 CompletionRegistry；ftre 425 passed、Core 238 passed，Inbox/Compaction wheel 构建通过 | 完成 F12/C2 迁移后的生命周期、重复 admission 和文档 Owner 清理 | F12.8、F12.9、F12.10 |
| 2026-08-23 | 修复执行中删除 Session 的竞态：删除路径等待 active Turn 收尾；ReplyProjection 在最终快照持久化成功前保留 active 状态；已删除 Session 不再发布空 `to_channel` 状态 | 日志暴露 `REPLY_END` 晚于 Session 删除，导致 `message 不存在`、助手回复丢失和空通道告警 | AC11、AC14、AC17、AC18；新增生命周期与投影回归测试 |
| 2026-08-24 | 修复 Inbox 与 Agent Runtime 的并发装载竞态：`ftre-inbox` 通过 Inject 显式声明 `agent_runtime` 依赖，确保 Inbox ACTIVE 前完成 admission handler 绑定；新增启动绑定回归断言 | 独立 Fiber 并发 settle 时，Inbox 可能先 ACTIVE 但未绑定 AgentLoop，客户端发送消息会收到 `inbox-unavailable` | AC1、AC5、AC14；全量测试 439 passed |
| 2026-08-24 | F17/F18 后续收敛：当前 Gateway 将 Inbox 作为必选 Plugin；Inbox 只拥有队列 Service/Hook/Worker，`send_message`、`task`、`team_*`/`wait_agent` 分别迁入三个业务 Package，Agent Runtime 删除 Inbox 透传 | 修复 `TurnExecutor._inbox` 未接线导致 `Injected("inbox")` 永远为 None，同时避免把使用 Inbox 的业务 Tool 误归入队列 Owner | F17/F18 PRD；不改变 Inbox Package 的独立发布能力 |
