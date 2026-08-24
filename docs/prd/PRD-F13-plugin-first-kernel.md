# PRD-F13 Plugin-first 内核收敛与消息交接

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F13 |
| 名称 | Plugin-first 内核收敛与消息交接 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-23 |
| 定稿日期 | 2026-08-23 |
| 验收日期 | 2026-08-23 |
| 前置阶段 | F12：Session Inbox 独立 Package 与消息协议 |
| 关联文档 | `docs/TODO.yaml`；`PRD-F1-backend-plugin-refactor.md`；`PRD-F6-agent-hook-runtime.md`；`PRD-F9-service-injection-and-debt-cleanup.md`；`PRD-F12-session-inbox-protocol.md` |
| 外部消费者 | `E:\binn\ftre-desktop`，只消费已冻结的消息与队列协议 |

## 1. 背景

F1、F6、F10、F12 已经建立 Cordis Composition、Service、Hook Runtime、独立
`ftre-compaction` 和独立 `ftre-inbox`。但是当前代码仍然存在两个问题：

1. “使用插件框架”不等于“功能已经插件化”。部分业务流程仍由 Gateway、AgentLoop、
   TurnExecutor 或 Bootstrap 手工协调；可选能力虽然有 `plugin.py`，运行时 Owner 仍散落。
2. 为解决局部问题继续增加 Coordinator、Port、Facade、转换对象和兼容状态，会让调用链
   更长，读代码时无法快速回答“谁拥有状态、谁决定时机、卸载谁可以关闭功能”。

因此，F13 先收敛架构规则，再以“用户输入从 Queue 交接到 Session History”为真实用例验证规则。
本阶段不追求一次拆出大量 PyPI 包，也不把每个函数包装成 Plugin。

## 2. 目标

### 2.1 总目标

冻结并落实以下原则：

> 内核只提供机制；业务能力由 Plugin 装配；有状态能力通过公开 Service 被 Inject；可选行为
> 通过 Hook 注册；关闭或卸载一个功能时，只需要关心它自己的 Plugin/Package。

### 2.2 可理解性目标

任意一项能力都必须能用三句话说明：

- 哪个 Plugin 创建并销毁它；
- 它提供哪个 Service，或者监听哪些 Hook；
- 哪些消费者通过 `inject` 使用它。

如果一项能力需要同时解释多个 Coordinator、Port、Facade 和全局 setter 才能回答上述问题，
视为架构债务。

### 2.3 消息交接目标

保持 F12 已冻结的数据语义：

```text
Admission 成功       -> QueueItem 是可恢复的 pending 事实
Inbox claim          -> 输入离开 pending，准备交给 Agent
Agent Turn 入口      -> UserMessage 进入正式 Session History
LLM / Tool 执行      -> 产生后续 Session Event
```

客户端必须连续展示同一条输入，但不能因此把尚未 claim 的 QueueItem 提前伪装成正式历史。

## 3. 不做什么

- 不新增通用 `AgentControlPort`、`MessageJournalPort`、`CompactionPort` 或同类接口层。
- 不新增 Admission Coordinator、Claim Coordinator 等只转发一次调用的对象。
- 不把 QueueItem、pending、capacity、placement 引入 AgentService 或 Agent Core。
- 不把 Session、Inbox、Agent 三个存储合并为一个巨型 Service。
- 不要求每个 Plugin 都独立发布 PyPI；先形成清楚的 Plugin 边界，Package 拆分按复用价值决定。
- 不在本阶段重写 `ftre-agent-core` 的 ReAct、LLM 或 Tool 算法。
- 不承诺跨 `inbox.json` 与 Session JSON 的 exactly-once 分布式事务；继续遵守 F12 的
  at-most-once claim 语义，并对失败给出明确诊断。
- 不恢复旧 Lane、MailboxStore、Legacy Plugin 或兼容导入。

## 4. 架构定义

### 4.1 Kernel

Kernel 只允许拥有以下机制：

```text
Kernel
├─ Context / Service 注册与解析
├─ Plugin Manifest / Discovery / Loader / Manager
├─ Hook 注册、作用域分发和取消
├─ Fiber / Effect 生命周期与资源清理
└─ 启动诊断、依赖缺失和冲突报告
```

Kernel 禁止 import 或识别以下业务词汇：

```text
QueueItem / next-turn / next-step / pending
Compaction / compact threshold
Command / slash command
Session title / Schedule / Team / MCP / Skill
WebSocket payload / FastAPI router
Agent prompt / Tool policy / Model selection
```

### 4.2 Service

Service 是一项稳定、有状态、可被消费的能力，例如：

- `sessions`：正式 Session 历史和查询；
- `agent`：执行单条 `InboundMessage`；
- `inbox`：pending、claim 和队列投影；
- `compaction`：压缩测量和执行；
- `commands`：命令注册与执行；
- `http`：路由注册表。

Service 可以是必选能力，也可以是可选能力。是否必选由 Composition 决定，不由 Service
在 import 时决定。

### 4.3 Plugin

Plugin 是装配和生命周期边界，而不是另一个业务模型：

```python
inject = ("sessions", "hook_runtime")
provide = ("example",)


def apply(ctx, config=None):
    service = ExampleService(ctx.sessions, config or {})
    ctx.provide("example", service)
    # Cordis effect executes the factory immediately and stores its disposer.
    ctx.effect(lambda: service.close, label="example:close")
```

一个 Plugin 可以：

- 提供一个 Service；
- 注册 Hook、Tool、Command 或 HTTP Router；
- 同时提供 Service 并注册自己的 Hook；
- 在卸载时通过 Effect 撤销全部贡献。

Plugin 不得：

- 直接构造另一个 Service 的实现类；
- import 另一个能力的 repository、runtime 私有目录；
- 通过全局变量、setter 或 Bootstrap 字段交换依赖；
- 在 Plugin 之外留下无法清理的 Task、Listener、Route 或线程。

### 4.4 Agent Runtime

Agent Runtime 是最小执行内核，但仍由 Provider Plugin 装配，不属于 Cordis Kernel。

它只负责：

```text
InboundMessage
    -> Turn
    -> Reasoning Step
    -> Prompt / LLM Stream
    -> Tool Dispatch
    -> Assistant Output
```

它可以 Inject：

- `sessions`；
- `tools`；
- `system_prompt`；
- `hook_runtime`；
- LLM adapter Service。

它不得知道：

- 输入来自 Queue、WebSocket、Schedule 还是 Team；
- Inbox 是否安装；
- Compaction 是否安装；
- Command 如何解析；
- Queue 的容量、位置、编辑、删除和重连协议。

Runtime 可以把 `inbox` 作为可选的运行时能力转交给需要异步派发的 Tool；这不等于
Runtime 拥有 Inbox。Admission、worker、claim、snapshot 和恢复仍只能由 `ftre-inbox`
Plugin/Service 实现，AgentLoop 不读取 QueueItem，也不决定 claim 时机。

## 5. 能力归属

| 能力 | Owner | 形态 | 消费方式 |
|---|---|---|---|
| Cordis Context、Loader、Hook 生命周期 | `kernel/plugins` | Kernel mechanism | Composition 使用 |
| Session 历史 | Session Provider Plugin | `sessions` Service | `inject=("sessions",)` |
| Agent 执行 | Agent Provider Plugin | `agent` Service | `inject=("agent",)` |
| Pending 队列 | `ftre-inbox` Plugin | `inbox` Service + worker + wire contribution | Inject 或 Hook |
| 上下文压缩 | `ftre-compaction` Plugin | `compaction` Service + Agent Hooks | Hook / Inject |
| Slash Command | Command Provider Plugin | `commands` Service + ingress contribution | Inject |
| Tool Registry | Tool Provider Plugin | `tools` Service | Inject；具体 Tool 由 Plugin 注册 |
| System Prompt | Prompt Provider Plugin | `system_prompt` Service | Inject；section 由 Plugin 注册 |
| Title、Schedule、Team、MCP、Skill | 各自 Feature Plugin | Hook/Tool/可选 Service | Inject / Hook |
| WebSocket、HTTP | Channel/HTTP Provider Plugin | 接入适配器 | Inject 公开 Service |

“内置”只表示默认 Composition 会加载，不表示可以绕过 Plugin 生命周期。

## 6. 目标消息流程

### 6.1 接入与 Command 旁路

```text
WebSocket / HTTP / Plugin Source
              |
              v
       Ingress Router
          |       |
          |       `-- slash command --> CommandService
          |
          `-- ordinary input --> InboxService
```

Ingress Router 只做协议归一化和公开 Service 调用。它不构造 SessionRepository、
InboxRepository 或 TurnExecutor。

### 6.2 Queue 到 Agent

```text
InboxService.followup / steer / inject
              |
              +-- durable QueueItem
              +-- session/queue snapshot
              `-- worker wake
                        |
                        v
                inbox/before-claim Hook
                        |
                        v
                atomic claim exact IDs
                        |
                        v
              AgentService.run(InboundMessage)
```

AgentService 的公开输入始终只有 `InboundMessage`。`QueueItem` 在 Inbox 边界转换后终止，
不得继续穿透到 Agent Runtime。

### 6.3 History 交接

Agent Runtime 在进入耗时的 Agent 构建、Prompt Assembly 和 LLM 之前，通过 Inject 的
`SessionService` 追加一次正式 UserMessage：

```text
AgentService.run(InboundMessage)
      |
      +-- sessions.append_user_input(inbound)
      |       `-- USER_MESSAGE / session event
      |
      `-- TurnExecutor.execute(inbound, user_message_id=...)
```

约束：

- 使用现有 `request_id` 作为 QueueItem 与 USER_MESSAGE 的相关性，不新增公开 ID 类型；
- SessionService 是正式历史的唯一 Owner；
- TurnExecutor 不再拥有普通用户消息的持久化逻辑；
- 追加失败时 Agent Turn 不启动，Inbox 记录失败诊断；本阶段不伪装成功；
- Confirmation、Command、Plugin context 继续走各自公开入口，不误记为普通用户消息。

### 6.4 客户端无空窗交接

```text
发送
  -> 本地 sending 项
  -> accepted ACK / session/queue：queued
  -> claim 快照：executing（客户端暂留同 request_id 项）
  -> USER_MESSAGE：替换为正式历史
```

客户端只能在以下事实到达时改变状态：

- ACK：Admission 成功；
- `session/queue`：当前 pending 完整快照；
- `USER_MESSAGE`：正式历史已提交；
- `session/status`：Agent active 状态。

客户端不得根据空队列猜测消息失败，也不得在 ACK 时伪造正式 UserMessage。

## 7. 功能需求

- [x] **FR1：Kernel 业务零知识**
  - Kernel 仅保留第 4.1 节机制；
  - 架构测试禁止 Kernel import Inbox、Compaction、Command、Session、Agent 业务实现；
  - Composition 只声明 Plugin，不手工拼装业务对象图。

- [x] **FR2：Plugin-first 装配**
  - 每项内置 Service 都由对应 Provider Plugin 创建、provide 和清理；
  - 默认必选能力也必须遵守 Plugin 生命周期；
  - 缺失可选 Plugin 时消费者通过 Hook 空链或 `ctx.get()` 明确降级，不触发 import error。

- [x] **FR3：Inject 是跨模块调用的唯一方式**
  - Service 之间通过 Cordis `inject` 获取公开 Service；
  - 禁止直接 import/实例化另一个 Service 的 provider、repository 或 runtime 实现；
  - 禁止新增全局 setter、bind_legacy、Bootstrap service bag 和重复 Composition Owner。

- [x] **FR4：拒绝无价值中间层**
  - 删除只做参数透传的 Coordinator、Port、Facade 和 compatibility alias；
  - 只有存在两个真实实现或明确的跨包契约时才新增 Protocol；
  - 单实现协作直接使用命名 Service 的窄公开方法。

- [x] **FR5：Agent Runtime 瘦身**
  - AgentService 只接收 `InboundMessage` 并负责 active Turn；
  - TurnExecutor 只负责 Turn/Reply/Tool 执行，不知道 Queue、Command 和 Compaction；
  - 可选行为只通过 Agent Hook 或 Inject Service 接入。

- [x] **FR6：功能 Plugin 化**
  - Inbox、Compaction、Command、Title、Schedule、Team、MCP、Skill 都有唯一 Plugin Owner；
  - 具体 Tool 通过 Tool Plugin 注册，不在 AgentService 静态构造；
  - System Prompt section 通过 Plugin 注册，不由 AgentLoop拼接 Feature 文本。

- [x] **FR7：Queue 与 History 分离**
  - Admission 只产生 QueueItem，不提前写 UserMessage；
  - claim 后、LLM 前由 SessionService 写入 UserMessage；
  - QueueItem 与 USER_MESSAGE 沿用同一 request_id 相关性；
  - TurnExecutor 删除普通输入的重复持久化 Owner。

- [x] **FR8：协议连续性**
  - 保持 `session.prompt`、`session/queue`、`session/status`、`USER_MESSAGE` 现有 envelope；
  - ACK、queue snapshot、status、history event 各自只有一种语义；
  - 客户端在 queue → history 交接期间不闪烁、不重复、不丢消息。

- [x] **FR9：卸载和缺失能力**
  - 卸载可选 Plugin 后不残留 Hook、Route、Task、Thread 或 Service 引用；
  - 未启用 Inbox 时普通 `session.prompt` 返回稳定 capability error，AgentService 仍可被直接调用；
  - 未启用 Compaction、Title、Schedule、Team、MCP、Skill 时基础 Agent Turn 正常运行。

- [x] **FR10：诊断与可读性**
  - Plugin diagnostics 能显示 provide/inject、pending、active、failed 和 unload 结果；
  - 关键类和非显然生命周期代码提供中文注释，解释 Owner、边界和失败语义；
  - 任何跨 Service 调用都能从 `inject` 声明和 Composition 清单追踪。

## 8. 实施切片

### F13.1 内核边界冻结

- 建立 Kernel 允许依赖清单和禁止业务词汇门禁；
- 固定 Service、Provider Plugin、Feature Plugin、Hook 的定义；
- 更新架构总览和 Composition 规则。

### F13.2 Service/Plugin Owner 审计

- 逐项审计 `services/`、`plugins/builtin/`、`packages/`；
- 记录每项能力的 Plugin Owner、Service key、inject 和生命周期；
- 找出直接实例化、全局 setter、service bag、重复 provider 和私有 import。

### F13.3 Agent 数据面收敛

- 将 UserMessage 写入移动到 AgentService 的 Turn 入口；
- 删除 TurnExecutor 的普通输入持久化职责和 `persist_input` 双路径；
- 保证 AgentService 仍只接收 `InboundMessage`。

### F13.4 可选行为 Plugin 化

- 清理 AgentLoop/TurnExecutor 对 Compaction、Command、Title、Schedule 等实现的直接依赖；
- 统一改为 Hook 或 Inject 的公开 Service；
- 不为一次性调用新增 Port/Coordinator。

### F13.5 Queue → History 协议交接

- 固定 request_id 相关性；
- 覆盖 queue claim 快于 USER_MESSAGE 的顺序；
- 验证 Desktop 无闪烁、无重复和重连一致性。

### F13.6 生命周期与缺失能力测试

- 验证 Plugin disable/unload/restart；
- 验证 Hook in-flight 取消和资源清理；
- 验证无 Inbox、无 Compaction、无 Feature Plugin 的最小 Composition。

### F13.7 债务清理与验收

- 删除死代码、兼容壳、空目录、缓存和陈旧测试替身；
- 更新 PRD、TODO、CHANGELOG 和执行报告；
- 完成全量测试、ruff、Gateway smoke 和客户端联调。

## 9. 验收标准

- [x] **AC1**：架构测试证明 Kernel 不 import 任何第 4.1 节之外的业务实现。
- [x] **AC2**：Composition Root 只声明 Plugin；Bootstrap 不手工创建业务 Service 或保存第二份
  Service 引用表。
- [x] **AC3**：每个目标能力都有唯一 Plugin Owner、稳定 Service key、明确 inject 和可逆 Effect。
- [x] **AC4**：AgentService/TurnExecutor 中不存在 QueueItem、pending、capacity、placement、
  Command parser 或 Compaction 实现依赖。
- [x] **AC5**：跨 Service 调用均通过 Inject 的公开能力；架构扫描不存在 provider/repository/
  runtime 私有实现跨模块 import。
- [x] **AC6**：无新增无价值 Port/Coordinator/Facade；已识别的透传层和兼容 alias 清理完成。
- [x] **AC7**：普通输入只写入一条 UserMessage，写入发生在 claim 后、LLM 前；TurnExecutor 不再
  重复持久化。
- [x] **AC8**：按 ACK、queue、claim、USER_MESSAGE、status 的合理乱序回放，Desktop 不出现
  “发送后消失再出现”、重复气泡或永久 optimistic 项。
- [x] **AC9**：禁用或卸载 Inbox、Compaction、Title、Schedule、Team、MCP、Skill 后，行为符合
  FR9，且无资源泄漏。
- [x] **AC10**：最小 Composition 只加载 Kernel、Session、Agent、LLM、Tools 时可以完成一个
  真实 Agent Turn。
- [x] **AC11**：新增/修改的公共 Service、Plugin 和生命周期逻辑具有中文边界注释；执行文档
  列出 Owner 审计结果与删除项。
- [x] **AC12**：`python -m pytest -q`、`python -m ruff check src tests`、`git diff --check`、
  Gateway smoke 和 Desktop `pnpm test`（含 Electron package 类型检查）通过；独立 renderer
  `tsc --noEmit` 的既有测试夹具错误不属于本后端阶段，已在执行报告中记录。

## 10. 测试计划

### 10.1 架构测试

- Kernel import allowlist；
- Composition Root 不实例化业务类；
- Service key 唯一 provide；
- inject 与私有 import 门禁；
- 禁止 Queue/Compaction/Command 词汇进入 Agent 数据面；
- 禁止 compatibility alias、全局 setter 和重复 Owner。

### 10.2 消息链路测试

- admission、duplicate request_id、queue snapshot；
- claim 后 UserMessage 写入一次；
- Session 写入失败时不启动 Agent Turn；
- claim snapshot 先于 USER_MESSAGE；
- reconnect、cancel、edit/remove/steer 竞争；
- Command 全程旁路 Inbox 和 TurnExecutor。

### 10.3 Plugin 生命周期测试

- load、unload、restart；
- 依赖缺失后 pending，依赖恢复后激活；
- in-flight Hook 取消；
- worker、route、listener、thread 和 service dispose；
- 最小 Composition 与全量 Composition。

### 10.4 回归测试

- Session CRUD、fork、search 和 persistence；
- Reply、Tool、System Prompt 和 Agent Hook；
- Compaction 启用/禁用；
- Schedule、Team、Subagent、MCP、Skill；
- WebSocket admission、queue、status、history；
- Desktop chat projection。

## 11. 风险与决策

| 风险/决策 | 处理 |
|---|---|
| 把“Plugin 化”误解为每个文件一个 Plugin | Plugin 按完整能力和生命周期划分；内部可以有多个协作文件 |
| 必选 Service 不是可选功能 | 必选能力仍由 Provider Plugin 装配，只是在默认 Composition 中 required |
| 为解耦继续增加接口类型 | 单实现直接 Inject 命名 Service；两个真实实现或跨包稳定契约才使用 Protocol |
| Queue 与 History 跨存储无法 exactly-once | 保留 F12 at-most-once，明确诊断；本阶段不引入分布式事务状态机 |
| Plugin 卸载时 active 操作仍在运行 | 依赖 Cordis Fiber/Effect 和 Hook 取消完成 drain，再撤销 Service |
| 一次迁移过大 | 按 F13.1-F13.7 分片，每片保持测试可运行，不保留长期双 Owner |

## 12. 变更记录

| 日期 | 变更内容 | 理由 | 受影响验收 |
|---|---|---|---|
| 2026-08-23 | 初始草案：尝试将 UserMessage 前移到 Admission，并引入消息交接协调 | 解决 queue claim 与 USER_MESSAGE 之间的客户端显示空窗 | 已被本次架构评审替代 |
| 2026-08-23 | 根据 DSH 源码修正为 Admission 保存 QueueItem、claim 后写入 UserMessage | pending 控制面不能提前污染正式 Session History | 保留为 FR7-FR8 的消息用例 |
| 2026-08-23 | 按用户架构目标整体重写为 Plugin-first 内核收敛：Kernel 只提供机制，功能由 Plugin/Service/Hook 拥有；删除 Coordinator/Port 扩张方案，消息交接作为真实验证用例 | 当前代码可理解性下降，局部修复不能继续扩大 Agent 与协调层；需要先固定 Owner、Inject 和生命周期边界 | 重写 FR1-FR10、AC1-AC12 和实施切片 |
| 2026-08-23 | 完成 Agent Runtime/Channel Provider、Service-owned HTTP 路由、Queue→History 交接、可选 Inbox 降级和 Effect 清理；补齐路由 Owner、无 Inbox、重启/卸载和公共名称边界测试 | 组合根不再手工创建业务对象或注册业务路由，所有能力通过 Plugin 生命周期接入 | FR1-FR10、AC1-AC12 已重核通过 |
| 2026-08-23 | 明确 AC12 的跨仓库验证边界：Desktop `pnpm test` 是本阶段联调门禁；独立 renderer tsc 报告既有 `awaitingEcho` 测试夹具缺字段，不在 ftre 后端范围内修复 | 避免把外部客户端既有类型债务伪装成后端验收结果 | AC12 证据口径收窄并记录 |
| 2026-08-24 | 审计修复 TraceService 双 exporter、AgentLoopRuntime 冗余 DTO、Inbox/Agent Runtime 旧实例绑定和 WebSocket 队列发布回调；新增 `inbox/changed` 与 `inbox/status-changed` Hook，并保持 ftre 核心不直接 import `ftre_inbox` | 消除重复状态/资源 Owner，确保 Inbox restart/unload 后工具、Loop 和 WS 都只解析当前 Service | AC3、AC5、AC6、AC9 已重跑通过 |
