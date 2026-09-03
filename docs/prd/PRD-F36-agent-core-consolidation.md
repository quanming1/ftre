# PRD-F36 Agent Core 合并与 Package 分层简化

> 本 PRD 的目标是消灭独立 `ftre-agent-core` 分发边界，把仍然有价值的 Agent
> 契约和执行算法分别归入 ftre 现有 Package，并删除重复 Owner、死事件和兼容壳。
> 它不是把 Core 目录原样复制到 `src/ftre`，也不是把 Agent Runtime 变成 Host Service。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F36 |
| 名称 | Agent Core 合并与 Package 分层简化 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-28 |
| 定稿日期 | 2026-08-28 |
| 验收日期 | 2026-08-28 |
| 关联文档 | `docs/TODO.yaml` F36；`docs/prd/PRD-F30-llm-service-package.md`；`docs/prd/PRD-F33-agent-package-final-architecture.md`；`docs/prd/PRD-F34-tool-service-runtime.md`；`docs/prd/PRD-F35-agent-service-inbox-message-boundary.md`；`AGENTS.md` |
| 配对工作 | `E:\ftre-agent-core` C8「Core Package Retire」已完成；`E:\binn\ftre-desktop` 事件协议清理已完成 |

---

## 1. 背景与目标

### 1.1 背景

当前系统已经完成 F30-F35 的主要边界建设，但仍存在一个历史遗留分层：

```text
E:\ftre-agent-core                  独立发行的算法库
        ↑
ftre-agent / ftre-agent-runtime      继续依赖 Core
        ↑
E:\ftre\src\ftre                    Host Service 和 Plugin
```

代码调研确认：

1. `ftre-agent` 和 `ftre-agent-runtime` 的 Package 结构已经建立，但仍声明
   `ftre-agent-core` 依赖。
2. `ftre` 及其 Package 约有 47 个 Python 文件直接导入 `ftre_agent_core`。
3. LLM 协议实现已经迁移到 `ftre-llm`；Core 的 `llm/base.py`、两个 OpenAI
   Adapter、Registry、BlockAssembler、wire 和错误类仍是重复 Owner。F36 的 LLM
   工作是删除重复实现并改调用方，不是再次把实现复制到第三个目录。
4. ToolService 已经拥有注册、作用域、schema 和执行能力，但仍以 Core
   `ToolRegistry` 作为内部实现。
5. Core Event 面包含没有生产者的 DataBlock、ToolResultData 和
   ExceedMaxIters 事件；Host CustomEvent 与 Agent StreamEvent 也混在同一层。
6. Core 的 `ReActAgent/ReActRunner` 与 `ftre-agent-runtime` 的 AgentLoop/
   TurnExecutor 不是两套等价循环：前者拥有 Reasoning → Acting → Exit、LLM
   attempt/retry、Permission、ToolHandler 和 AgentState，后者拥有 Session/Turn
   编排、Admission、Projection、维护屏障和 Host Service 注入。F36 必须把两层
   合并成“一个 Agent 算法 Runtime + 一个 Host 编排入口”，不能简单复制目录。

### 1.2 目标

完成本阶段后：

```text
ftre-agent
└─ AgentService + 稳定公共契约

ftre-agent-runtime
└─ AgentLoop + ReAct 执行实现

ftre-llm
└─ LLM Service、协议适配器和 StreamChunk

ftre Host
└─ Session / Tool / Profile / Prompt / Inbox 等有状态业务 Service

ftre-agent-core
└─ 不再是 ftre 的运行时依赖和发行边界
```

### 1.3 成功标准

- ftre 生产代码、测试、Package 配置中不再出现 `ftre_agent_core` import。
- `ftre-agent`、`ftre-agent-runtime`、`ftre-llm` 可以独立构建和安装。
- AgentService、Agent Runtime、LlmService、ToolService 各自只有一个 Owner。
- 普通消息、Steer、Tool、Confirmation、Retry、Fallback、Compaction、取消和
  Session 恢复行为保持现状。
- 无兼容 alias、无旧路径转发模块、无第二套 Registry、无第二套 Retry Loop。
- 没有真实生产者的事件被删除，并同步清理客户端协议处理。

### 1.4 非目标

- 不把所有代码搬入 `src/ftre/services/`。
- 不把 Session、Channel、MessageBus、Inbox、Compaction、MCP 或 Workspace
  逻辑放入 `ftre-agent`。
- 不把 Agent Retry Loop 搬入 LlmService。
- 不在本阶段重写 LLM Provider 的请求协议。
- 不改变客户端的消息视觉布局；事件删除只做必要的类型和 reducer 清理。
- 不保留 `ftre_agent_core` 兼容包、`sys.modules` 映射或 deprecated 代理入口。
- 不在运行中的 Gateway 上执行 kill 或重启；验证使用独立测试进程和 Gateway
  smoke 实例。

### 1.5 功能需求（FR）

以下需求是 F36 的可交付范围；每条都必须映射到至少一个阶段、测试和最终验收项：

- [x] FR1：`ftre-agent` 提供稳定的 AgentService、AgentHandle、Create/Resume/Run/
  Stream/Cancel/Status/Reservation 契约；契约不引用 Inbox、Channel、Session
  Repository 或 Runtime 私有类型。
- [x] FR2：`ftre-agent-runtime` 成为唯一 ReAct 执行 Owner，合并
  Reasoning → Acting → Exit、ToolCall 调度、MessageContext、RunState 和取消语义；
  工具权限/审批/执行流水线由 ToolService 与 ApprovalService 拥有，Host AgentLoop
  只保留 Turn/Session 编排。
- [x] FR3：AgentService 的状态由 Runtime 快照单向推进；`run()` 与 `stream()` 共用
  Admission、busy、reservation、异常回滚和终态通知，不允许第二套 active Task
  推断。
- [x] FR4：`AgentService.stream()` 输出真实有序 AgentStreamEvent；不得用单个伪造
  `run.completed` 替代文本、思考、工具、Retry、确认、用量和 ReplyEnd 事件。
- [x] FR5：`ftre-llm` 是唯一 LLM Service、Adapter、StreamChunk、BlockAssembler、
  wire 和错误归一化 Owner；Runtime、Compaction、Title、Recovery、Fallback
  不得实例化 Provider Adapter。
- [x] FR6：Retry 只由 Runtime 执行，`llm/error` 只作失败决策；Fallback 只在最后
  一次 attempt 且零协议输出时通过 `llm/stream` 接管，不能重复执行或吞掉原错误。
- [x] FR7：`ftre-agent` 拥有 Msg、ContentBlock、AgentStreamEvent、ToolDefinition、
  Permission 数据模型；Session JSON、Inbox JSON 和客户端 wire 字段保持兼容。
- [x] FR8：ToolService 是唯一工具注册、作用域、schema、注入、单次执行和结果
  归一化 Owner；Runtime 只消费不可变 ToolView，不拥有 Registry、MCP Client 或
  具体 callable。
- [x] FR9：Session/Inbox/Compaction/Messaging/Task/Team/MCP/core-tools 等消费者
  全部切换到新公共契约；测试与生产代码同批迁移，禁止旧 import 假绿。
- [x] FR10：CustomEvent、Pipeline 和 Compaction 维护事件移出 Agent 公共流，改用
  Host typed event；无真实生产者的 DataBlock 流事件、ToolResultDataDelta 和
  ExceedMaxItersEvent 与客户端 reducer 一起删除。
- [x] FR11：Trace 模型保持公共纯实现，SQLite/文件存储仍由 Host Trace Plugin
  拥有；Tracing 不把数据库连接或全局状态带入 Runtime。
- [x] FR12：`ftre-agent`、`ftre-agent-runtime`、`ftre-llm`、Inbox、Compaction
  和根 ftre wheel 可以在不安装 `ftre-agent-core` 的洁净环境中独立构建和安装。
- [x] FR13：所有迁移阶段保留普通消息、Thinking、Tool Call、Confirmation、Steer、
  Retry、Fallback、Compaction、取消、恢复和多 Provider replay 行为。
- [x] FR14：禁止兼容 alias、deprecated re-export、sys.modules 映射、旧 Registry
  桥接和第二套 Retry/状态机；旧实现必须在 F36.7/F36.8 物理删除。
- [x] FR15：跨仓库改动可在不 kill/restart 当前 Gateway 的前提下验证；Core C8 和
  Desktop 事件清理分别通过自己的 PR，不在 F36 中本地合并外部仓库。

### 1.6 非功能需求

- **可安装性**：Package 的 import 图必须为单向 DAG；`ftre-llm` 不依赖 Agent，
  Runtime 不依赖 Host 私有模块，洁净 venv 不得从工作区源码路径偷加载 Core。
- **可观察性**：每次 Agent Run 的事件 sequence、Retry attempt、Tool call、
  状态快照和错误码可关联；Compaction/Host 维护事件不得伪装成 Agent 回复。
- **取消与资源安全**：取消必须传播到 LLM、Tool、Confirmation 等 await 点；
  Plugin disposer、MCP 连接、临时 View、PreparedLlmCall 和 tracer span 不得泄漏。
- **数据安全**：Hook payload 不含 API Key、原始异常对象和完整 Prompt；ToolView
  不暴露函数对象、Registry、MCP client 或可变 allow/deny 引用。
- **兼容性**：迁移阶段保持 Session JSON、Inbox JSON、Provider 请求形状和 Desktop
  已有保留事件；协议删除必须有 producer/consumer 扫描和客户端同步提交。
- **性能**：不引入额外重试或重复序列化；`AgentService.stream()` 应在事件产生时
  增量转发，不等待整轮完成后再发结果；Tool 并发和 LLM stream 的背压语义保持。
- **交付安全**：每阶段独立提交、测试、扫描和执行记录；验证只能使用独立测试
  进程/fake provider，不操作运行中的 Gateway。

---

## 2. 当前 Owner 与架构债务

### 2.1 当前代码归属

| 当前内容 | 当前 Owner | 问题 |
|---|---|---|
| AgentService | `packages/ftre-agent` | 已基本正确，仍依赖 Core 的 Msg/Hook 类型 |
| AgentLoop/TurnExecutor | `packages/ftre-agent-runtime` | 已是 Host Runtime，但底层 ReAct Runner 在 Core |
| ReActAgent/ReActRunner | `E:\ftre-agent-core` | 与 Runtime 存在跨仓库算法边界 |
| LLM Adapter | Core + `packages/ftre-llm` | 重复实现，`ftre-llm` 应成为唯一 Owner |
| Tool Registry | Core + `src/ftre/services/tools` | ToolService 已是业务 Owner，但实现仍依赖 Core |
| Msg/Event/Hook | Core | 被 Session、Inbox、Compaction、Runtime、客户端共同消费 |
| Session 持久化/Projection | `src/ftre/services/session` | Host Owner 正确 |
| Tool 注册/作用域/MCP | `src/ftre/services/tools` + Plugin | Host Owner 正确 |
| Retry 执行循环 | Core ReasoningExecutor | 当前唯一 Retry Loop，必须保持唯一 |

### 2.2 源码审查证据（2026-08-28）

> 本节记录的是 F36 开工前的基线快照，用来解释迁移决策；完成后的 Owner、删除
> 清单和验证结果以第 3、4、8 节及执行报告为准，不应把其中的“当前”描述理解为
> 现行代码状态。

以下结论来自逐文件阅读，而不是只根据目录名称推断。迁移实施时必须以这些
调用关系为基线；若代码已变化，先更新本节和变更记录，再改阶段任务。

#### 2.2.1 Agent 数据流与状态边界

```text
ftre_inbox.InboxService
  └─ _to_agent_request(QueueItem) → AgentService.run(agent_id, AgentRunRequest)
       └─ AgentService.run()
            ├─ 检查 _entries[agent_id].state
            ├─ 写 running / run_id，并消费 RunReservation
            └─ runtime_handle.run(request)
                 └─ AgentLoop.run_input(RuntimeInput)
                      ├─ _direct_reservations / _direct_tasks / _maintenance
                      ├─ TurnExecutor.resolve_inbound_config()
                      ├─ TurnExecutor.execute() → Turn(BUILDING…)
                      └─ TurnExecutor._create_agent() → Core ReActAgent
                           └─ Core ReActRunner → ReasoningExecutor / ToolHandler / ExitExecutor
```

当前存在三处运行态：

| 状态 | 代码位置 | 实际内容 | F36 处理 |
|---|---|---|---|
| Agent 公开投影 | `packages/ftre-agent/.../service.py:_AgentEntry` | state、run_id、reservation | 保留公开快照；只由 Runtime 生命周期回调驱动 |
| Host Turn 状态 | `packages/ftre-agent-runtime/.../state.py:Turn` | inbound、配置、取消、confirm、message_id | 保留，属于 Host 编排；不向公共契约泄漏 |
| ReAct Run 状态 | Core `agent/runner/_state.py:RunState` | iteration、reply、token、retry、done_reason | 搬到 Runtime，作为唯一算法状态 |

当前 `AgentService` 的 `run/stream/cancel` 和 `AgentLoop` 的
`_direct_tasks/_direct_reservations` 都会写忙碌状态。F36.5 必须把它改成单向
关系：Runtime 是 active Run 事实 Owner，AgentService 仅保留 identity、配置
快照和由 Runtime 回调更新的公开状态。Inbox reservation 仍可由 AgentService
提供原子准入，但不能再复制 active Task。

另外，当前 `AgentService.stream()` 与 `run()` 还不具备完全相同的 Admission
语义：`stream()` 没有调用 `_factory_or_raise()`，也没有消费 `RunReservation`，
并且 `AgentLoopHandle.stream()` 在 Runtime 完成后只生成一个伪造的
`run.completed` 信封。F36.5 必须一次性修正三点：

1. `run()` 和 `stream()` 共用同一个“校验 Agent/Session → 检查 busy → 消费
   reservation → 设置 running → Runtime 执行”的入口；拒绝原因和状态回滚一致；
2. `stream()` 的事件来自 Runtime 的真实 `AgentStreamEvent`，按 sequence 单调
   递增，异常/取消也必须有明确的结束事件或结果；禁止用成功事件掩盖失败；
3. Service 在 `ReplyEnd`/Runtime 结束后再发布终态快照，监听器看到的状态不得早于
   最后一条 Agent 事件，也不得因异常遗留 `running`。

#### 2.2.2 真实的 Core 调用点

| Core 入口 | 当前调用方 | 迁移后 Owner |
|---|---|---|
| `ReActAgent(...)` | `ftre_agent_runtime.factory.create_core_agent` | `ftre-agent-runtime.react_agent` |
| `ReActAgent.run()` | `ftre_agent_runtime.turn_executor._run` | Runtime `AgentRun` |
| `ReActRunner._loop()` | Core 内部 | Runtime `react_runner.py`，唯一 Reason/Act/Exit 循环 |
| `ReasoningExecutor.stream()` | Core 内部 | Runtime `executors/reasoning.py` |
| `ActingExecutor._execute_calls()` | Core 内部 | Runtime `executors/acting.py` |
| `ExitExecutor.stream()` | Core 内部 | Runtime `executors/acting.py` 或 `executors/exit.py` |
| `ToolHandler.run_one/spawn/gather_results` | Core 内部 | 调度 → Runtime `tool_calls.py`；执行 → Host ToolService |
| `MessageContext.*` | Core Runner、Host Turn | Runtime context；消息类型来自 `ftre-agent` |
| `PermissionEngine.evaluate()` | Core ActingExecutor | Host ToolService `permission.py`；规则模型来自 `ftre-agent` |

`ftre_agent_runtime/factory.py` 当前仍名为 `create_core_agent`，并直接导入
`AgentState/ReActAgent/PermissionContext`；这是 F36.5 的首个强制替换点。迁移
完成后生产代码中不得再出现 `create_core_agent`、`ftre_agent_core.agent` 或
`ftre_agent_core.permission`。

#### 2.2.3 LLM 已迁移但仍有重复实现

`packages/ftre-llm/src/ftre_llm/service.py` 已实现：

- `register_adapter/list_providers/resolve_model_info/prepare_call/stream/close`；
- `PreparedLlmCall` 一次性使用、`max_retries=0`、取消和凭据隔离；
- `ftre_llm.events.StreamChunk`、`BlockAssembler`、OpenAI Completions/Responses
  适配器和 wire 归一化；
- `LlmServiceAdapter` 将旧的 `stream(messages, tools)` 形状桥接到
  `LlmRequest`，并关闭重复 `llm/stream` dispatch。

Core 仍有第二套 `llm/base.py`、`llm/adapters/*`、`llm/registry.py`、
`llm/block_assembler.py`、`llm/wire/*` 和 `llm/errors.py`。因此 F36.2 不是
重新设计 LLM Service，而是先证明两个适配器行为等价，再让 Runtime 直接使用
`ftre_llm.LlmRequest/StreamChunk/LLMError`，最后删除 Core 副本。

需要特别保留的协议事实：Core 旧 `LLMAdapter.stream(messages, tools)` 与
`ftre_llm.LlmAdapter.stream(request)` 签名不同；迁移期间只能有一个显式
`LlmServiceAdapter`，不得在 Runtime 再写第三个适配器。`llm/stream` 是惰性
流包装 Hook，`llm/error` 是一次 attempt 失败后的决策 Hook；二者都不能被
改造成 LlmService 内部 Retry Loop。

#### 2.2.4 ToolService 当前已拥有的能力和缺口

`src/ftre/services/tools/service.py` 当前公开 `register/restrict/register_view_preparer/
snapshot/get/schemas/prepare_view/execute`，并负责 owner/source/scope、scoped
shadow、allow/deny 和 Plugin disposer。缺口是：

1. `_registry` 和 `prepare_view()` 返回值仍是 Core `ToolRegistry`；
2. `filtering.py` 仍以 Core Registry 的 `names/unregister` 为实现前提；
3. Core `ToolHandler` 直接调用 `registry._resolve_injections()`、`registry.get()`
   和 `registry.execute()`，实际绕过了 ToolService 的端口；
4. `Tool`/`Injected`/`ToolParameter` 目前由 Core 定义，所有内置和业务 Package
   都因此依赖 Core。

F36.4 的目标不是再造一个 Runtime Registry，而是把注入、schema、可见性和单
   次执行收敛在 ToolService 的 scoped `ToolView` 中；Runtime 只调用
   `ToolView.execute()` 并负责并发、取消、权限和 Agent 事件。

#### 2.2.5 事件真实生产/消费矩阵

| 事件族 | 当前生产者 | 当前消费者 | 结论 |
|---|---|---|---|
| Reply/Model/Text/Thinking/Tool/Confirm | Core Runner → Runtime → `SessionProjection`/Desktop | Session、Desktop、Trace | 迁移后保留 |
| `RetryEvent` | Core `ReasoningExecutor` | Desktop retry UI、Session/Trace | 保留，生产者迁移到 Runtime |
| `UserMessageEvent` | `AgentLoop`、`SessionEventService` | SessionProjection、Desktop | 保留；属于 Agent→Host 边界 |
| `HintBlockEvent` | Core Reasoning/Acting/Exit、Host Tool | Session/Runtime、Desktop 隐藏渲染 | 先保留，后续再评估是否内部化 |
| `CustomEvent` | Runtime pipeline、Compaction | SessionProjection、Desktop `CUSTOM` reducer | 从 AgentStream 移出，改 Host typed event |
| `DataBlockStart/Delta/End` | 当前生产代码无命中 | Desktop reducer 仍有分支 | 先清客户端分支，再删除类型 |
| `ToolResultDataDelta` | 当前生产代码无命中 | Desktop reducer 仍有分支 | 同上，不能只删后端类型 |
| `ExceedMaxItersEvent` | 当前生产代码无命中 | Desktop reducer 仍有分支 | 用 `ReplyEnd.finished_reason`，同步删客户端分支 |

`DataBlock` 内容模型不能删除：它仍用于图片输入和 `HintBlock` 多模态内容；
可删除的是三个“流事件”类型，不是内容块本身。

### 2.3 依赖方向目标

```text
ftre-agent-runtime ──depends──> ftre-agent
ftre-agent-runtime ──depends──> ftre-llm
ftre Host ──depends──> ftre-agent
ftre Host ──depends──> ftre-agent-runtime
ftre Host ──depends──> ftre-llm

ftre-agent ──depends (contracts only)──> ftre-llm
ftre-agent ──must not depend──> ftre-agent-runtime
ftre-agent-runtime ──must not depend──> ftre Host
ftre-llm ──must not depend──> ftre-agent-runtime
ftre-llm ──must not depend──> ftre-agent
```

`ftre-agent` 是门面和稳定契约，不导入 Runtime；它对 `ftre-llm` 只依赖
`LlmStreamPayload` 等纯契约，不创建 LLM Client。Runtime Plugin 通过
`AgentService.register_factory()` 暴露实现；Host 通过 Composition 装配。

---

## 3. 终局 Package 结构

### 3.1 `ftre-agent`：公共门面与跨包契约

```text
packages/ftre-agent/
└─ src/ftre_agent/
   ├─ service.py              # AgentService / AgentHandle
   ├─ contracts.py            # AgentCreateSpec / RunRequest / RunResult / StreamEnvelope / View
   ├─ registry.py             # Agent identity / Hook scope carrier
   ├─ config.py               # AgentConfig / LLMConfig 纯数据模型
   ├─ hooks.py                # HookSpec/Dispatcher + Agent/Tool/LLM Hook 契约
   ├─ types.py                # ReplyFinishedReason 等无状态值类型
   ├─ tracing.py              # Tracer/TraceRun/TraceSpan 纯实现（不含存储）
   ├─ message/
   │  ├─ _block.py             # ContentBlock、Data/Hint/Tool 状态模型
   │  ├─ _msg.py               # Msg / UserMsg / AssistantMsg / append_event
   │  └─ _convert.py           # 领域消息与 Provider-neutral mapping 转换
   ├─ event/
   │  └─ _event.py             # Agent 公共流事件与输入确认事件
   └─ tool/
      ├─ definition.py         # ToolDefinition / ToolParameter / Injected（只构造声明）
      ├─ contracts.py          # ToolCallRequest / ToolContext / ToolExecutionResult / ToolView
      └─ permission.py         # PermissionRule/Request/Decision 数据模型
```

它只放需要被 Session、Inbox、Tool Plugin、Agent Runtime、Compaction 和第三方
Plugin 共同导入的稳定类型，不放 Session Repository、Inbox 队列、LLM Client、
Agent Loop 或后台 Task。`ToolDefinition` 是跨包声明，不等于 ToolService；
ToolService 拥有注册索引、作用域投影、注入、执行和卸载入口。插件应显式提交
`ToolDefinition + implementation`，定义模块不得执行 callable 或持有 Service
状态。`Tracer` 是纯内存追踪实现，不拥有 SQLite 或其它持久化资源。

### 3.2 `ftre-agent-runtime`：唯一 Agent 执行 Owner

```text
packages/ftre-agent-runtime/
└─ src/ftre_agent_runtime/
   ├─ plugin.py               # Runtime Provider Plugin
   ├─ engine.py               # AgentLoop
   ├─ turn_executor.py        # Host Turn 生命周期
   ├─ react_agent.py          # 迁移后的 Agent Runtime 外壳
   ├─ react_runner.py         # 迁移后的唯一 ReAct 状态机
   ├─ executors/
   │  ├─ reasoning.py         # LLM 流、Retry、LLM Error Hook
   │  └─ acting.py             # ToolCall 解析、Confirmation 和结果消费
   ├─ tool_calls.py           # 多 Tool 并发/串行调度、顺序提交和取消排空
   ├─ message_context.py      # Agent memory → LlmRequest 的准备
   ├─ run_state.py            # ReAct RunStatus、attempt 和终态
   ├─ state.py                # Host Turn 私有状态
   └─ runtime_factory.py      # Runtime Factory 与 Provider 装配
```

如果迁移后 `engine.py` 和 `react_runner.py` 仍然各自维护完整状态机，视为
失败；`engine.py` 只负责 Host Turn/Session 编排，`react_runner.py` 只负责一次
Agent Run 的纯算法。Runtime 不新增 `ToolRegistry`，Tool View 由 ToolService
创建并作为端口注入。`AgentService` 的状态是 Runtime 状态的只读投影，不能再
独立推断一套 active Task。

### 3.3 `ftre-llm`：唯一 LLM Service Owner

```text
packages/ftre-llm/
└─ src/ftre_llm/
   ├─ service.py
   ├─ contracts.py
   ├─ events.py
   ├─ block_assembler.py
   ├─ errors.py
   ├─ wire/normalize.py
   └─ adapters/
      ├─ openai_completions.py
      └─ openai_responses.py
```

该 Package 提供一次调用，不提供 Agent 级 Retry、Session 状态或 Tool 执行。

### 3.4 ftre Host Service

```text
src/ftre/services/
├─ session/                  # Session 身份、消息持久化、Projection
├─ tools/                    # ToolService：注册、作用域、schema、执行
│  ├─ service.py             # contribution 索引、scope 投影、注入、执行、disposer
│  ├─ permission.py          # PermissionEngine 与 allow/deny/ask 门禁
│  └─ approval.py            # ApprovalService 与用户确认端口
├─ agent_profile/            # Agent 配置和私有文件解析
├─ system_prompt/            # Prompt section 组装
├─ messaging/                # MessageBus / Channel
├─ workspace/
└─ attachments/
```

Host Service 不得反向 import Runtime 私有类，不得直接实例化 AgentLoop 或
`tool_calls.py` 调度器；Runtime 通过端口消费 Host Service。

### 3.5 生命周期与销毁 Owner 矩阵

| 对象 | 创建者 | 事实 Owner | 销毁时机 | 禁止的第二 Owner |
|---|---|---|---|---|
| `AgentService` | `ftre-agent` Provider | Agent identity、Factory、公开投影、Reservation | Host Composition close | Runtime 不得创建第二个 AgentService |
| `AgentLoop` | `ftre-agent-runtime` Provider | active Turn、Runtime EventSink、维护屏障 | Runtime Plugin dispose；先 cancel/wait active | AgentService 不得持有同义 active task |
| `TurnExecutor/Turn` | `AgentLoop` | 单个 Host Turn 的配置、Session、确认和收尾 | Turn 完成/取消/错误后 | ReActRunner 不得持有 Session Repository |
| `RuntimeAgent/ReActRunner` | Runtime Factory | 单次 Reasoning/Acting/Exit 算法与 `RunState` | Run 结束或暂停后释放 | AgentLoop 不得复制 ReAct 状态机 |
| `ToolView` | `ToolService.prepare_view()` | 一次 Run 的可见工具快照和执行端口 | Run 结束、Tool Plugin 卸载 | Runtime 不得缓存全局 Registry/Callable |
| `PreparedLlmCall` | `LlmService.prepare_call()` | 一次 attempt 的 Adapter 句柄 | stream 完成/异常/取消后 | Adapter 不得自行 Retry；Runtime 不得缓存句柄 |
| `Tracer/TraceSpan` | TraceService/Runtime | 单次 Run 的追踪上下文 | ReplyEnd/取消/错误后 flush | Runtime 不得拥有 SQLite/文件连接 |

所有对象必须有可测试的 `create → use → close` 顺序；关闭阶段应幂等，重复
`dispose/close` 不得产生第二次取消、第二次 Hook 注销或幽灵状态通知。

---

## 4. 分阶段实施计划

每个阶段都必须完成：代码、测试、架构扫描、专项验证和独立提交。阶段完成后才能
进入下一阶段；禁止长期新旧双轨。

### F36.1：基线、Owner 和事件矩阵冻结

**目标**：在改代码前锁定依赖图、真实生产者、真实消费者和删除条件。

**工作内容**：

1. 统计所有 `ftre_agent_core` import、Package dependency 和 entry point。
2. 建立 `Core module → target package → consumer` 迁移矩阵。
3. 对每个 Event 建立 `producer / backend consumer / desktop consumer / delete gate`。
4. 冻结 `ftre-agent`、`ftre-agent-runtime`、`ftre-llm`、ToolService 的 Owner。
5. 标记所有旧入口、兼容 alias、转发模块和重复 Registry。
6. 为每一个待迁移模块记录“输入、输出、可变状态、外部依赖、测试文件、删除
   条件”，不能只记录文件名。例如 `MessageContext` 的输入是 `list[Msg]`，输出
   是 Provider message mapping；`ToolHandler` 的输入是 `ToolCall + RunState`，
   输出是 `ToolResult + AgentStreamEvent`。
7. 画出两条必须保持的时序：普通消息
   `Inbox admission → AgentService.run → TurnExecutor → ReActRunner → SessionProjection`
   和失败路径 `StreamChunk(error) → LLMError → llm/error → Retry/Fallback → ReplyEnd`。
8. 将 `ftre-agent-core` 仓库的当前分支、未提交文件和待建 C8 记录登记为跨仓库
   前置条件；F36 不得误删另一个工作区的用户改动。

**输入**：当前两仓源码、`pyproject.toml`、F33/F34/F35 PRD、桌面端 reducer。

**输出**：迁移矩阵、事件矩阵、目标依赖图、删除清单。

**迁移矩阵至少包含以下行**：

| Core 模块 | 目标 Owner | 直接消费者（当前代码） | 迁移动作 |
|---|---|---|---|
| `message/_block.py`、`message/_msg.py`、`message/_convert.py` | `ftre-agent.message` | Session、Inbox、Compaction、Messaging、Runtime | 原样保留 wire 字段，先换 import，再补契约测试 |
| `event/_event.py` | `ftre-agent.event` + Host Event | SessionProjection、Desktop、Runtime | Agent 事件迁移；Custom/维护事件拆出 |
| `hooks.py` | `ftre-agent.hooks` | Kernel、Runtime、Recovery、Fallback、Tool | HookSpec/Hook payload 只保留一份 |
| `tool/base.py` | `ftre-agent.tool.definition` | core-tools、MCP、Task、Team、Messaging | 定义迁移；执行归 ToolService |
| `tool/registry.py` | `src/ftre/services/tools` | ToolService、Runtime `tool_calls.py` | 重写为 ToolService 私有索引和执行流水线；Runtime 不拥有 Registry |
| `permission/*` | `src/ftre/services/tools/permission.py` + public data types | ToolService、AgentState | 算法归 ToolService 执行前门禁，规则模型归公共契约 |
| `agent/runner/*` | `ftre-agent-runtime` | TurnExecutor、AgentLoop | 合并唯一 ReAct 状态机 |
| `llm/*` | `ftre-llm`（已完成 Owner） | Runtime、Compaction、Title | 证明等价后删除 Core 副本 |
| `tracing.py` | `ftre-agent.tracing` | Runtime、TraceService、TraceStore | 纯追踪模型与存储适配分离 |

### F36.1 文件级迁移台账（基于当前工作区扫描）

当前基线扫描到 `E:\ftre\src` 与 `E:\ftre\packages` 共 47 个 Python 文件含有
`ftre_agent_core` 引用，其中 `src` 25 个、Package 22 个；另有直接依赖声明的
Package 为 9 个。F36.1 不要求一次性修改这些文件，而是为每个文件确定唯一的
后续切片和验证责任：

| 当前文件组 | 当前作用 | 目标切片 | 必须完成的动作 |
|---|---|---|---|
| `packages/ftre-agent/src/ftre_agent/{contracts,hooks}.py` | 公共请求/结果/Hook 仍引用 Core | F36.3 | 改为 `ftre_agent.message/event/hooks`，删除 Core 类型 re-export，并验证 `__module__` |
| `packages/ftre-agent-runtime/src/ftre_agent_runtime/{engine,factory,state,turn_executor}.py` | Host Runtime 创建和消费 Core Agent/Msg/Event | F36.3、F36.5 | 先替换公共模型，再把 `create_core_agent`、Runner 和 Permission 移入 Runtime |
| `packages/ftre-compaction/src/ftre_compaction/service.py` | Compaction 消费 Core Msg/CustomEvent | F36.3、F36.6 | 消息改用 `ftre-agent`，维护事件改用 Host typed event |
| `packages/ftre-inbox/src/ftre_inbox/models.py` | QueueItem 携带 Core Msg | F36.3 | QueueItem 保持 Inbox 私有，内部 Msg 改为 `ftre-agent.message.Msg` |
| `packages/ftre-llm-recovery/src/ftre_llm_recovery/{plugin,policy}.py` | 失败决策订阅 Core `llm/error` | F36.3 | 改订阅公共 Hook；保留 RetryPolicy 的决策职责 |
| `packages/ftre-llm-fallback/src/ftre_llm_fallback/*` | `llm/stream` 零输出 fallback | F36.2、F36.3 | 只使用 `ftre-llm` 请求和公共 Stream Hook，不创建适配器 |
| `packages/ftre-messaging/src/ftre_messaging/send_message.py`、`ftre-task`、`ftre-team` | 业务发送/任务/协作工具依赖 Core Tool/Msg | F36.3、F36.4 | 仅导入公共定义，执行统一走 ToolService/ToolView |
| `src/ftre/kernel/hooks/spec.py`、`src/ftre/services/llm/hooks.py` | Kernel/Host 组装 Hook | F36.3 | Kernel 只消费公共 Spec；Host 文件只保留 `agent/request` 等 Host 专有 Hook |
| `src/ftre/services/session/{entity/state,events,message/converter,persistence/repository,projection,service}.py` | Session 持久化、Projection、事件聚合 | F36.3、F36.6 | Msg/Event 路径迁移；CustomEvent 改为 Host typed event；保持 JSON/wire 不变 |
| `src/ftre/services/tools/{filtering,hooks,service}.py` | ToolService 仍以 Core Registry 为内部实现 | F36.4 | 以 ToolView 替代 Registry；过滤、schema、execute 共享同一可见性投影 |
| `src/ftre/plugins/builtin/{command,core_tools,mcp,plan,schedule,skill,trace}` | 内置工具、MCP、Trace Plugin 的 Core 消费者 | F36.3、F36.4、F36.6 | Tool 定义迁到 `ftre-agent`，执行回 ToolService；Trace 存储留 Host |

每个表格行完成时，提交必须同时包含：代码变更、对应测试、旧引用扫描结果、
删除/保留理由和可回滚点。测试文件中旧 import 必须与生产文件同批切换，不能以
排除测试目录的扫描结果作为阶段完成证明。

### F36.1 Core 源码逐文件落点

下面是“拆分后落到哪里”的完整规则。除明确标记为“删除”的内容外，不允许把
`ftre-agent-core` 整个目录复制到 ftre，也不允许在新包中保留同名兼容转发模块。

| Core 源文件/目录 | 拆分后的落点 | 保留内容 | 删除或改写内容 |
|---|---|---|---|
| `agent/react.py` | `packages/ftre-agent-runtime/src/ftre_agent_runtime/react_agent.py` | RuntimeAgent/ReActAgent 构造、run/cancel、状态注入 | 公共 AgentService 门面、Core 包路径 |
| `agent/runner/react_runner.py` | `.../react_runner.py` | 唯一 Reasoning → Acting → Exit 循环 | 对 Session/Inbox 的直接访问 |
| `agent/runner/_execute_reasoning.py` | `.../executors/reasoning.py` | LLM stream 消费、attempt、Retry、`llm/error`、空响应恢复 | Core LLM Adapter/第二套重试 |
| `agent/runner/_execute_acting.py` | `.../executors/acting.py` | ToolCall 解析、调用 ToolService、Confirmation 状态和结果消费 | Permission 求值、直接操作 Registry |
| `agent/runner/_state.py` | `.../state/run.py` | RunState、Reasoning、Acting、Exit、TurnResult | 公共 Service 状态投影 |
| `agent/runner/tool_handler.py` | 调度 → `.../tool_calls.py`；执行流水线 → `E:\ftre\src\ftre\services\tools\executor.py` | Agent 级并发/串行、取消排空、结果顺序；Service 级注入/Hook/归一化 | 不保留同名整体 Handler，避免 Runtime 再拥有工具执行 Owner |
| `message_context.py` | `.../message_context.py` | Msg → LlmRequest 的上下文组装、tool result 配对 | Provider Adapter 细节 |
| `permission/_engine.py` | `E:\ftre\src\ftre\services\tools\permission.py` | ToolService 执行前的纯 PermissionEngine.evaluate 算法 | AgentState/Session 的隐式读取、Runtime 私有权限 Owner |
| `permission/_context.py`、`_types.py` | `packages/ftre-agent/src/ftre_agent/tool/permission.py` | PermissionRule/Request/Decision/Context 数据模型 | Runtime 算法和副作用 |
| `state/_agent_state.py` | `.../state/agent_state.py` | 可恢复 `context`、`permission_context` | 公共 AgentService 状态 |
| `message/_block.py`、`_msg.py`、`_convert.py` | `packages/ftre-agent/src/ftre_agent/message/` | Msg、ContentBlock、Provider-neutral 转换、JSON discriminator | 对 Core 包名的 re-export |
| `event/_event.py` | Agent 事件 → `packages/ftre-agent/src/ftre_agent/event/`；Host 事件 → `E:\ftre\src\ftre\services\session\events.py` | Reply/Model/Text/Thinking/Tool/Confirm/Retry/UserMessage | CustomEvent、无生产者事件按 F36.6 删除 |
| `hooks.py` | `packages/ftre-agent/src/ftre_agent/hooks.py` | HookSpec/Dispatcher/Agent、Tool、LLM payload/spec | Core 依赖、Host Hook 注册表 |
| `types.py` | `packages/ftre-agent/src/ftre_agent/types.py` | ReplyFinishedReason 等无状态值类型 | Agent/Runtime 行为 |
| `tool/base.py` | 声明模型 → `ftre-agent/tool/definition.py`；schema/introspection → `E:\ftre\src\ftre\services\tools\schema.py`；执行 → `...\services\tools\executor.py` | ToolDefinition/ToolParameter/Injected | `Tool.execute()`、线程池、全局 Registry 逻辑不进入公共契约 |
| `tool/registry.py` | ToolContext/ToolExecutionResult → `ftre-agent/tool/contracts.py`；注册、作用域、快照 → `src/ftre/services/tools/service.py`；注入/执行/归一化 → `src/ftre/services/tools/executor.py` | ToolView 所需的最小数据和端口 | `ToolRegistry` 类、公共 `_tools`、Runtime Registry |
| `tool/cancellation.py` | `ftre-agent/tool/contracts.py` 的取消 Protocol + Runtime `asyncio.Event` | 取消信号的语义 | Core `CancellationToken` 全局实现和全局线程资源 |
| `tracing.py` | 纯模型 → `packages/ftre-agent/src/ftre_agent/tracing.py`；Exporter → `src/ftre/plugins/builtin/trace/` | TraceRun/TraceSpan/Tracer 接口 | Core SQLite/文件副作用 |
| `llm/base.py`、`adapters/`、`registry.py`、`block_assembler.py`、`wire/`、`errors.py` | 已存在的 `packages/ftre-llm/src/ftre_llm/` 对应模块 | LlmService、两种 OpenAI 协议、StreamChunk、错误归一化 | Core LLM 副本；不再新增第三份 Adapter |
| `llm/utils.py` | 默认删除；如需原始调用日志，放入 Host Trace/LLM observability Plugin | 可选、显式启用的诊断能力 | Core `LLMLogger` 文件写入和硬编码日志目录 |
| `threading.py` | 删除；同步工具使用 ToolService 注入的 executor/`asyncio.to_thread` | 无需迁移全局线程池 | 进程级 `thread_pool` 和隐式资源 Owner |
| 各目录 `__init__.py` | 各目标包自己的显式公共入口 | 真实 Owner 的 `__all__` | 旧路径 re-export、deprecated alias、兼容空壳 |

关键拆分结论：`ftre-agent.tool.definition` 只描述“工具是什么”，
`ToolService` 才负责“工具是否可见、由谁贡献、如何注入、如何执行、如何卸载”。
定义模块不能有 Registry、scope、allow/deny、Plugin disposer、MCP 连接或
`execute()`；否则视为与 ToolService 重叠，F36.4 不得通过。

**验证**：

```powershell
rg -n --glob '*.py' 'ftre_agent_core' E:\ftre\src E:\ftre\packages
rg -n --glob '*.py' 'CustomEvent|DataBlock|ExceedMaxIters|RetryEvent' E:\ftre\src E:\ftre\packages
rg -n --glob 'pyproject.toml' 'ftre-agent-core' E:\ftre
rg -n --glob '*.{ts,tsx}' 'DATA_BLOCK_|TOOL_RESULT_DATA_DELTA|EXCEED_MAX_ITERS|CUSTOM' E:\binn\ftre-desktop\packages
```

**提交**：`docs(prd): 冻结 Agent Core 合并边界`。

### F36.2：LLM Owner 收敛

**目标**：确认 `ftre-llm.LlmService` 和 `ftre-llm` Adapter 已经是唯一 LLM 实现，
并把 Core 中的重复实现降为可删除状态。

**工作内容**：

1. 对照 Core `llm/base.py`、Completions/Responses Adapter、`BlockAssembler`、
   `LLMError.classify` 和 `wire.normalize`，逐条核对 `ftre-llm` 对应实现；差异必须
   先写成测试，不允许以“看起来一样”通过。
2. 保持 Runtime 过渡期唯一桥接：`LlmServiceAdapter` 接收旧的
   `stream(messages, tools)`，内部创建 `LlmRequest`，并把 `StreamChunk` 原样交回；
   不在 Runtime 或 Host 再写 `OpenAIAdapter`。
3. 确认 Completions/Responses 的 thinking、tool-call、usage、取消、finish/error
   和 Responses reasoning item replay 行为在新 Service 上都有回归测试。
4. 将 `StreamChunk`、`BlockAssembler`、`LLMError`、`LlmRequest` 的生产代码导入
   统一到 `ftre-llm`；Runtime 只能依赖公开接口，不能依赖 `ftre-llm.adapters.*`
   的私有实现。
5. 暂不删除 Core 文件：只有在 F36.3/F36.5 完成所有 import 替换后，F36.7 才能
   删除 Core 的 `llm/adapters`、`llm/registry.py`、`llm/base.py`、
   `llm/block_assembler.py`、`llm/wire`、`llm/errors.py`、`llm/utils.py`。
6. 保持 Retry 在 Runtime/Agent 执行层；`LlmService` 不添加第二套 Retry。Fallback
   只能通过 `llm/stream` 包装零输出的最后一次 attempt。

**不变约束**：

- `llm/stream` 仍是一轮调用的惰性流包装 Hook；Spec 最终由公共 Hook 契约提供，
  Service 只负责 dispatch。
- `llm/error` 仍由 Runtime 在一次 attempt 失败后触发；Spec 和 payload 不再由 Core
  提供，Recovery/Fallback 通过公共契约订阅。
- Adapter 的 `max_retries=0` 保持不变。

**验证**：

```powershell
python -m pytest -q packages/ftre-llm/tests packages/ftre-agent-runtime/tests
python -m ruff check packages/ftre-llm packages/ftre-agent-runtime
```

必须覆盖：completions、responses、thinking、tool call、usage、cancel、协议错误。

**提交**：`refactor(llm): 收敛 LLM Adapter 到 ftre-llm`（只提交等价验证和调用方
切换；Core 删除留到 F36.7，避免中途把 ReAct 运行打断）。

### F36.3：公共契约迁移到 `ftre-agent`

**目标**：消灭业务代码对 `ftre_agent_core.message/event/hooks` 的依赖。

**迁移内容**：

```text
Core message/*       → ftre-agent.message/*
Core event/*         → ftre-agent.event/*
Core hooks.py        → ftre-agent.hooks.py
Core types.py        → ftre-agent.types.py
Core tracing.py      → ftre-agent.tracing.py
Core tool/base.py    → ftre-agent.tool.definition.py
Core tool registry models → ftre-agent.tool.contracts.py（只迁移 ToolCall/Result/View 协议）
```

**工作内容**：

1. 先建立新模块和显式 `__all__`，并为每个 Pydantic discriminator 写 round-trip
   测试。新类型的 `__module__` 必须指向 `ftre_agent.*`，不能通过 re-export
   伪装迁移完成。
2. 将 `HookMode/HookScope/HookFailurePolicy/HookSpec/HookDispatcher` 和
   `agent/*`、`tool/*`、`llm/error`、`llm/stream` 的 payload/spec 迁入
   `ftre-agent.hooks`。`src/ftre/kernel/hooks/spec.py` 只从这里导入，不再从 Core
   导入；`src/ftre/services/llm/hooks.py` 只保留 Host 专有的 `agent/request` 和
   `llm/adapters-updated` 组装。
3. 迁移 Session、Inbox、Compaction、Messaging、Task、Team、MCP、core-tools、
   Recovery、Fallback、Command、Trace Plugin 的 import；同时更新各 Package 的
   `pyproject.toml`。当前至少有 9 个 pyproject 直接声明 Core 依赖，不能只改
   `ftre-agent` 和 Runtime。
4. 迁移 Runtime 的事件、消息、Tool、Permission 数据模型；Runtime 的内部
   `RunState/Turn` 仍是私有状态，不导出为公共契约。
5. 更新序列化、Pydantic discriminator、Session JSON、Inbox JSON、Tool schema
   和客户端事件字符串，保持现有 wire 字段和值不变；这是“路径迁移”，不是协议
   重命名阶段。
6. 迁移 `TraceRun/TraceSpan/Tracer` 到公共纯实现，`SQLiteTraceExporter` 仍留在
   Host Trace Plugin；`TraceService.build_tracer()` 只创建一个绑定 Host exporter
   的 Tracer，不把数据库逻辑带进 Runtime。
7. Core 只允许在迁移期间存在于独立分支；禁止在 `ftre-agent` 新增
   `from ftre_agent_core import ...`、`sys.modules` alias、deprecated re-export
   或名称相同的转发文件。

**关键契约**：

```python
from ftre_agent import AgentRunRequest, AgentRunResult
from ftre_agent.event import AgentStreamEvent, UserMessageEvent
from ftre_agent.message import Msg, UserMsg, AssistantMsg
from ftre_agent.tool import ToolDefinition, ToolParameter, Injected, ToolView
from ftre_agent.hooks import LLM_ERROR_SPEC, LLM_STREAM_SPEC
```

**验证**：

```powershell
python -m pytest -q tests/contracts tests/architecture packages/ftre-inbox/tests packages/ftre-compaction/tests
rg -n --glob '*.py' 'ftre_agent_core\.(message|event|hooks|types|tool)' E:\ftre
rg -n --glob '*.py' 'from ftre_agent_core|import ftre_agent_core' E:\ftre\src E:\ftre\packages
```

两条命令在本阶段结束时都必须没有命中；若历史文档命中，使用
`--glob '!docs/**'` 的生产扫描再次确认。所有活动测试必须切换到新路径，不能
保留“旧导入测试”作为假绿。

**提交**：`refactor(agent): 迁移 Agent 公共契约`。

### F36.4：ToolService 与 Tool Runtime 解耦

**目标**：ToolService 成为工具管理唯一 Owner，Runtime 只消费隔离的工具视图。

**工作内容**：

1. 将 `ToolDefinition`、`ToolParameter`、`Injected` 作为 `ftre-agent` 公共定义；
   Plugin 负责从函数签名构造声明并把 `implementation` 一并提交给 ToolService，
   公共定义不负责注册、scope、注入或执行。
2. 将 `ToolService` 的 `register/get/snapshot/schemas/execute/prepare_view`
   变成唯一管理面。
3. 删除 `ToolService` 对 Core `ToolRegistry` 的 import。
4. `prepare_view()` 返回 `ftre-agent.tool.ToolView` Protocol 的不可变快照，不返回
   Core/Runtime Registry。该 View 必须提供：`names`、`schemas()`、`get(name)`、
   `execute(name, arguments, context)`；`execute` 可返回值或 awaitable，注入和
   `ToolExecutionResult` 归一化由 ToolService 完成。
5. `ToolService` 保持 owner/source/scope、allow/deny、MCP 和 Plugin disposer；
   scoped shadow、global fallback 和 restriction 的解析只实现一次。
6. `ToolService` 拥有完整的单次工具执行流水线：权限决策、`tools/pre-execute`、
   单调 guard、ApprovalService ask、`tools/execute`、Injected 注入、同步/异步
   callable、结果校验/归一化、`tools/post-execute`、finalizeContent 和
   `tools/result`。这些阶段只能在 Service 内实现一次。
7. Runtime 的 `tool_calls.py` 只负责 Agent 级并发/串行分组、顺序提交、取消排空
   和把 ToolResult 喂回下一轮；它通过 `ToolView.execute()` 调用 Service，不能
   读取具体 callable 或复制权限流水线。Core `ToolHandler` 不作为整体文件保留。
8. `ToolService` 私有索引内部可以是 dict，但不得命名为公共 `ToolRegistry`；不得
   复制一份 `names/unregister/_resolve_injections` 给 Runtime。MCP 连接对象只
   能由 MCP Plugin/View Preparer 持有。

**目标调用链**：

```text
Tool Plugin
  → ToolService.register(ToolDefinition, implementation, owner, scope)
  → ToolService.prepare_view(agent_id, session_id, profile)
  → ToolView（names + schemas + execute）
  → Runtime tool_calls.py（并发/顺序调度）
  → ToolView.execute()
  → ToolService 权限/审批/执行/归一化流水线
```

**验证**：

- global/scoped/scoped shadow 行为一致；
- `schemas()` 和 `execute()` 使用同一可见性投影；
- 卸载 Plugin 后工具、Hook、disposer 全部消失；
- Runtime 源码中不存在 `ToolService._registry`、MCP Manager 或具体 Tool import；
- Runtime 只依赖 `ToolView` Protocol；静态扫描不得出现 `ToolRegistry(`、
  `_resolve_injections`、`registry.execute`、`tools/pre-execute` 或
  `ApprovalService` 具体实现 import；
- ToolService 的单次执行流水线顺序固定为 pre → guard → approval → around
  execute → definition.execute → normalize → post → finalize → result；
- Tool Hook、取消、超时、失败和并发测试全部通过。

**提交**：`refactor(tools): 解除 ToolService 对 Core Registry 的依赖`。

### F36.5：Agent Runtime 迁移与唯一状态机 Owner

**目标**：把 Core 的 ReAct 算法迁入 `ftre-agent-runtime`，并与现有 AgentLoop 合并，
不产生两套 Agent 状态机。

**迁移内容**：

```text
Core agent/react.py                 → runtime/react_agent.py
Core agent/runner/react_runner.py   → runtime/react_runner.py
Core _execute_reasoning.py          → runtime/executors/reasoning.py
Core _execute_acting.py             → runtime/executors/acting.py
Core runner/_state.py               → runtime/state/
Core permission/_engine.py          → E:\ftre\src\ftre\services\tools\permission.py
Core permission/_context.py/_types.py → ftre-agent/tool/permission.py
Core message_context.py             → runtime/message_context.py
Core tracing.py                     → ftre-agent/tracing.py（Runtime 只消费 `Tracer`）
Core runner/tool_handler.py         → Runtime `tool_calls.py` 调度 + Host ToolService 执行流水线
```

**实际迁移步骤**：

1. `ReActAgent`、`ReActRunner`、三个 Executor、`RunState`、`MessageContext` 和
   ToolCall 调度逐个搬入 Runtime，并逐个替换相对导入；PermissionEngine、审批
   和工具执行流水线不搬入 Runtime，而是通过 `ToolView`/Host Service 端口消费；
2. 保留当前 Host `AgentLoop`/`TurnExecutor` 的 Admission、Session、Prompt、
   Projection、维护屏障和取消等待语义；它们不再创建“Core Agent”，而是创建
   Runtime 自己的 `RuntimeAgent`。
3. 将 `create_core_agent()` 重命名为 `create_runtime_agent()` 并删除旧名字；
   构造参数从 `tool_registry` 改为 `tool_view`，从 `AgentState` 读取的持久上下文
   仍由 Runtime `AgentState` 承载。
4. 把 Core `ReActRunner._loop()` 作为唯一 Reasoning → Acting → Exit 状态机；
   `AgentLoop.run_input()` 只驱动一个 `TurnExecutor`，不得再实现第二套空响应、
   Retry 或 stop-decision 分支。
5. 在 Runtime 中保留 `ReasoningExecutor` 的 attempt 循环和 `RetryEvent` 产出：
   `max_attempts = 1 + max_retries`；每次 attempt 失败先构造 `LLMError`，触发
   `llm/error`，再由 Runtime 按 decision、取消和硬上限决定 Retry/Stop。Fallback
   只在 `llm/stream` 发现“最后一次 attempt 且零协议输出”时接管。
6. 让 `MessageContext` 只操作 `ftre_agent.message.Msg`，将 Provider mapping
   封装为 `ftre_llm.LlmRequest` 的输入；Responses 原始 Output Item 仍只能存放
   在 Msg metadata，不能进入可见 ContentBlock。
7. 修正当前 `AgentLoopHandle.stream()` 的行为：它现在等待 `run()` 后只发一个
   `run.completed` 假事件。迁移后必须提供 `AgentStreamEnvelope` 真实事件流（文本、
   思考、工具、模型用量、Retry、确认、ReplyEnd），并保证 `AgentService.stream()`
   在流结束后再更新公开状态。具体实现必须由 Runtime 建立每个 `run_id` 的
   `RuntimeEventSink`/有界异步队列：`publish_agent_event()` 同时写 Session 投影
   和该 Run 的 sink，`stream()` 在启动执行前订阅 sink，收到显式终止哨兵后结束；
   `run()` 与 `stream()` 共享同一执行 Task，不能因为两个入口各调用一次
   `run_input()` 而重复执行 Agent。队列满时按取消信号中断生产者，不能无限缓存。
8. 统一状态更新方向：Runtime 在 `RUNNING/PAUSED/FINALIZING/COMPLETED/
   CANCELLED/ERROR/COMPACTING` 边界发布状态快照；AgentService 只接受快照并
   通知监听器。删除 AgentService 中基于 `entry.state` 的第二套 active Task
   推断，但保留 `RunReservation` 作为 Inbox 入场原子门禁。
9. `AgentState.permission_context` 的数据格式保持 JSON 兼容；ToolService 的
   `permission.py` 按最高 priority、冲突 DENY、默认行为求值，ASK 交由注入的
   ApprovalService 处理；确认恢复仍从 Session 持久化 `ToolCallState` 重建，
   不依赖进程内 Runner。
10. Runtime 独立测试不加载 ftre Host；所有 Host 能力通过 `ports.py` Protocol
    或构造参数注入，禁止在 Runtime 内 `ctx.get()`、导入 `ftre.services.*` 或
    直接访问 Session/Inbox Repository。

**Owner 规则**：

- `AgentService` 只管理 Agent identity、Factory、Handle、公开状态投影和入口；
  不拥有 ReAct Task，也不复制 Runtime 的 active Task 状态。
- `AgentLoop` 只管理 Session/Turn 的 Host 编排。
- `ReActRunner` 只管理一次 Agent Run 的 Reasoning/Acting/Exit 算法。
- `ReasoningExecutor` 仍拥有一次 Reasoning 内的 Retry 循环。
- `tool_calls.py` 只消费 Tool View，不管理全局工具注册、权限或 callable；注入、
  审批、执行和归一化调用回到 ToolService 的 `ToolView.execute()`。
- Runtime 不 import `ftre.services.*` 的私有模块，只通过 Inject Service 使用能力。

**必须消除的重复**：

```text
禁止：AgentLoop 自己实现一套 Reasoning/Acting 状态机
禁止：ReActRunner 自己维护 Session/Inbox/Compaction 队列
禁止：AgentService 依据自己的 `_entries.state` 再推断一套 Runtime active 状态
禁止：Runtime 直接 new ToolRegistry 或 LLM Adapter
```

**验证**：

- AgentService 通过 Runtime Factory 创建、恢复、运行和取消；
- `AgentService.stream()` 能收到真实 Agent 事件，而不是仅有 `run.completed`；
- 同一 `run_id` 的 `run()`/`stream()` 只能启动一次 Runtime Task；断开 stream 不会
  隐式取消 Agent，显式 `cancel()` 才会中断执行；
- sink 队列有界、结束哨兵可达，事件 sequence 不重复、不倒退、不跨 Run 串线；
- 普通消息、Steer、Tool、Confirmation、Retry、Fallback、Compaction、取消和
  Session 恢复回归通过；
- 同一 Session 仍最多一个 active Turn；
- Runtime 不依赖 `InboundMessage`、Channel、MessageBus、Session Repository；
- Core ReAct 代码删除后，Runtime 仍能独立构建并执行 fake LLM/fake Tool 测试。

**提交**：`refactor(agent-runtime): 合并 ReAct Runtime 到 Agent Package`。

### F36.6：事件面简化与 Host 事件分离

**目标**：只保留真实 Agent 数据面事件，将 Host Pipeline/Compaction 事件移出 Agent
公共流，并删除没有生产者的事件。

#### 保留事件

```text
REPLY_START / REPLY_END
MODEL_CALL_START / MODEL_CALL_END
TEXT_BLOCK_START / TEXT_BLOCK_DELTA / TEXT_BLOCK_END
THINKING_BLOCK_START / THINKING_BLOCK_DELTA / THINKING_BLOCK_END
TOOL_CALL_START / TOOL_CALL_DELTA / TOOL_CALL_END
TOOL_RESULT_START / TOOL_RESULT_TEXT_DELTA / TOOL_RESULT_END
REQUIRE_USER_CONFIRM
USER_MESSAGE
RETRY
```

`USER_CONFIRM_RESULT` 保留为 Agent 输入命令，不加入输出事件联合类型。

#### 删除事件

```text
DataBlockStartEvent
DataBlockDeltaEvent
DataBlockEndEvent
ToolResultDataDeltaEvent
ExceedMaxItersEvent
EventType 枚举
```

删除条件：Core、ftre、桌面端和活动测试中没有真实生产/消费，或已完成对应
替代协议迁移。最大迭代通过 `REPLY_END.finished_reason` 表达。

#### Host 事件迁移

`CustomEvent` 不再属于 Agent 公共 StreamEvent。迁移为：

```text
SessionMaintenanceEvent
├─ context_compact_start
├─ context_compact_done
└─ context_compact_failed

PipelineEvent
├─ PIPELINE_START
├─ COMMAND_MATCHED
├─ TURN_START
└─ TURN_END
```

它们由 SessionEventService/Host EventBus 消费，不进入 Agent 公共契约。

**实现顺序和边界**：

1. 先新增 `HostPipelineEvent` 与 `SessionMaintenanceRecord` 两个 Host 内部模型，
   由 `SessionEventService` 提供 `emit_pipeline()`、`emit_maintenance()`；
   `SessionProjection` 直接消费这两个模型，不再通过 `CustomEvent.name` 字符串
   猜测语义。
2. `TurnExecutor._emit_step()` 改为调用 `SessionEventService.emit_pipeline()`；
   `CompactionService` 改为调用 `emit_maintenance()`。两者仍保持现有先投影再
   广播的顺序和 attach 快照语义。
3. WebSocket 线协议在客户端同步切换为 `PIPELINE_EVENT`、
   `SESSION_MAINTENANCE`（字段含 `name/value/reply_id`）；在客户端切换完成前，
   不删除旧 `CUSTOM` reducer 分支。禁止在 Agent 公共 Event 联合类型中保留一个
   “临时兼容 CustomEvent”入口。
4. `USER_MESSAGE` 不是 Host Pipeline 事件：它继续由 Agent/Session 边界产生，
   必须先持久化，再广播；Steer 的 `previous_assistant_message_id` 语义不变。
5. `ExceedMaxItersEvent` 删除前，确保 `ReplyEndEvent.finished_reason` 的 JSON
   值为 `exceed_max_iters`，客户端按结束原因显示，而不是依赖独立事件。
6. 删除事件时同时删除 `__init__` 导出、`EventType` 成员、测试 fixture、客户端
   reducer/type、文档和字符串常量；只删除类定义会留下“幽灵协议”。`EventType`
   不得只从导出列表移除：保留事件的 `EventBase.type` 改为稳定字符串字面量或
   `Literal`，由事件类自身声明；Core/ftre/Desktop 测试改为比较稳定值或事件类，
   不再依赖一份全局枚举。

**验证**：

- 桌面端 reducer、类型和测试同步清理删除事件；
- SessionProjection 仍能完成 Reply 聚合、UserMessage 投影和 Compaction 投影；
- Retry、Confirmation、UserMessage 和模型用量仍能正常渲染；
- `AgentStreamEvent` 中不再出现 Host CustomEvent。

**提交**：`refactor(events): 删除无生产者事件并分离 Host 事件`。

### F36.7：移除 Core 依赖、发行和仓库退休

**目标**：完成最终切换，删除旧分发边界。

**工作内容**：

1. 从根 `ftre` 和所有直接声明 Core 的 Package（当前为
   `ftre-agent`、`ftre-agent-runtime`、`ftre-compaction`、`ftre-inbox`、
   `ftre-llm-fallback`、`ftre-llm-recovery`、`ftre-messaging`、`ftre-task`、
   `ftre-team`）的 `pyproject.toml` 删除 `ftre-agent-core`，补齐新的最小依赖。
   同时检查 `ftre-agent` 的 `plugin.py`：公共 contracts/message/event/tool 模块
   导入不能传递加载 Cordis；若 Provider 入口继续使用 Cordis，必须把该依赖隔离
   在插件入口并用“仅导入公共契约”的洁净测试证明。
2. 更新 `pytest` pythonpath、构建脚本、README、CHANGELOG 和锁文件；清理
   `ftre_agent_core` 在 CLI 日志颜色表、架构说明、运行时 docstring 中的陈旧名称。
3. 删除 Core 旧目录、桥接、兼容 alias、迁移脚本、临时 fixture 和空目录；任何
   需要继续支持的外部协议必须在新公共 Package 中有真实 Owner，不能留下代理壳。
4. 构建并在洁净虚拟环境安装 `ftre-agent`、`ftre-agent-runtime`、`ftre-llm`、
   `ftre-inbox`、`ftre-compaction` 和根 `ftre` wheel；安装顺序不能依赖工作区
   的 `E:\\ftre-agent-core\\src`。
5. 创建独立的 Core 仓库配对阶段 C8「Core Package Retire」，退休
   `ftre-agent-core` 的发布入口；在 Core 仓库完成自己的 C8 PRD 和最终删除提交
   后，才删除远端仓库/发布版本。F36 不能代替 C8，也不能物理删除用户未授权的
   外部仓库。

**最终扫描**：

```powershell
rg -n --hidden --glob '!**/.git/**' --glob '!docs/**' --glob '!**/tests/**' --glob '!README*' --glob '!AGENTS.md' 'ftre_agent_core|ftre-agent-core' E:\ftre\src E:\ftre\packages E:\ftre\pyproject.toml E:\ftre\.github E:\ftre\scripts
rg -n --glob '*.py' 'create_llm_handler|OpenAICompletionsAdapter|OpenAIResponsesAdapter' E:\ftre\packages\ftre-agent-runtime E:\ftre\src
rg -n --glob '*.py' 'ToolService\._registry|ToolService\.registry|ToolRegistry\(' E:\ftre\src E:\ftre\packages
```

第一条命令必须无输出；README/AGENTS/CHANGELOG 中允许出现明确标注“已退休”的历史说明；第二条只能命中新 Package 的 Adapter 注册/测试，不能命中
Runtime 或 Host 的工厂调用；第三条不能命中 Runtime，ToolService 内部若使用私有
dict 必须不暴露 `ToolRegistry` 名称。历史文档和执行报告另行扫描并更新，不得用
排除参数掩盖生产残留。

**提交**：`chore(packaging): 移除 ftre-agent-core 发行依赖`。

### F36.8：重构收尾审计与跨仓库交付门禁

**目标**：证明“旧实现已删除、唯一 Owner 已落地、运行中行为没有回退”，并把
F36 与 Core C8、Desktop 事件清理的交付关系固定下来。

**收尾清单**：

1. 通读所有迁移后的生产文件，删除 `create_core_agent`、`Core Agent`、
   `legacy`、`deprecated`、`compat` 等只为过渡保留的命名、注释和空模块；
   文档中描述历史的内容必须明确标注为历史，不得让运行时继续依赖它。
2. 检查 `__pycache__`、临时 wheel、旧 build 目录、未跟踪迁移脚本和空 package
   目录；只清理 F36 产生且已确认不属于用户的文件。
3. 对 `AgentService`、`AgentLoop`、`TurnExecutor`、`RuntimeAgent`、
   `ToolService`、`LlmService` 各画一条“谁创建、谁拥有、谁销毁”的生命周期
   证据；发现一个对象有两个 Owner 就退回 F36.4/F36.5 修正。
4. 执行跨仓库协议检查：Ftre 的消息/Event JSON 与 Desktop reducer；Core C8
   的删除列表与 ftre 新 import；任何一侧仍引用旧模块都不能进入发布。
5. 在不触碰当前运行 Gateway 的前提下，使用独立临时目录、fake LLM、fake Tool
   和临时 SessionStore 完成 smoke；运行中的任务不得被 kill、restart 或迁移。
6. 生成执行报告，记录每个阶段的提交、测试命令、扫描结果、未完成的跨仓库门禁
   和下一步；只有报告、PRD、TODO、CHANGELOG 四者一致时才允许提 PR。

**阶段完成判据**：

- F36.1–F36.7 的所有 AC 勾选并有命令输出；
- ftre 源码/活动测试不再导入 `ftre_agent_core`；
- Ftre、Desktop、Core C8 三方删除清单一致；
- 所有旧兼容壳和第二 Owner 已物理删除；
- 当前运行中的 Gateway 未被本阶段操作。

**提交**：`docs(prd): 完成 Agent Core 合并审计`；随后按 Git Flow 推送 feature
分支并创建 PR，禁止本地合并到 develop。

---

### AgentService 公共 API 与 Runtime Port

F36 不改变调用方看到的主方法名，但会收紧每个方法的 Owner 和状态语义。公共
Service 只暴露以下入口：

| 方法 | 入参 | 出参 | 事实 Owner |
|---|---|---|---|
| `start` | 无 | `None` | AgentService 生命周期 |
| `close` | 无 | `None` | AgentService；触发 Runtime dispose 由 Provider 完成 |
| `register_factory` | `AgentRuntimeFactory` | `FactoryRegistration` | AgentService 注册槽位 |
| `create` | `AgentCreateSpec` | `AgentHandle` | identity 在 AgentService；运行句柄在 Runtime |
| `resume` | `AgentResumeSpec` | `AgentHandle` | Session checkpoint 读取在 Runtime/Session |
| `run` | `agent_id, AgentRunRequest` | `AgentRunResult` | Runtime 运行，Service 更新投影 |
| `stream` | `agent_id, AgentRunRequest` | `AsyncIterator[AgentStreamEnvelope]` | Runtime 真实事件流，Service 只转发/收尾 |
| `cancel` | `agent_id, reason` | `bool/CancelResult` | Runtime active Turn；Inbox pending 由 Inbox 处理 |
| `status/get/list` | `agent_id/session_id` | `AgentView`/状态字符串/只读列表 | Service 投影，不读取队列 |
| `is_busy/is_session_busy` | `session_id` | `bool` | Runtime active/maintenance + Service reservation |
| `try_reserve/release_reservation` | agent/session/request/lease | `RunReservation?`/`bool` | Admission 原子门禁 |
| `resume_confirmation` | session/channel/events/metadata | `AgentRunResult` | Runtime Confirmation 恢复 |
| `delete_session` | session_id | `None` | Runtime 等待 active 收尾后委托 Session 删除 |
| `on_created/on_disposed/on_status_changed` | listener | disposer | Service 观察面 |

`AgentRuntimeFactory` 在 F36.5 后只需实现 `create/resume` 和 Runtime 生命周期
端口；`cancel_session/get_session_status/is_active_session/delete_session` 等
当前 Factory 兼容方法要么归入 `RuntimeControlPort`，要么删除，不能让 AgentService
同时依赖两套同义接口。最终 `AgentService` 不知道 `AgentLoop`、`TurnExecutor`、
`QueueItem`、`SessionRepository` 的具体类型。

当前 `ftre-agent.contracts.AgentEvent` 的 `data: Mapping[str, Any]` 是为了伪事件
临时存在的宽泛信封。F36.5 必须将它收敛为可校验的流信封，不能继续用任意字典掩盖
事件类型：

```python
@dataclass(frozen=True, slots=True)
class AgentStreamEnvelope:
    agent_id: str
    run_id: str
    sequence: int
    event: AgentStreamEvent
```

`AgentRuntimeHandle.stream()` 和 `AgentService.stream()` 均返回
`AsyncIterator[AgentStreamEnvelope]`；`run()` 仍返回最终 `AgentRunResult`。如果
WebSocket 需要 JSON，序列化只能在 Host/Channel 边界完成，Runtime 不拼装客户端
字段。旧 `AgentEvent.data`、`run.completed` 伪事件和仅在 Service 内部转换的
宽泛字典必须在 F36.5 同批删除。

Runtime 的流端口最小契约如下，具体队列实现不进入 `ftre-agent` 公共包：

```python
class RuntimeEventSink(Protocol):
    async def publish(self, envelope: AgentStreamEnvelope) -> None: ...
    async def subscribe(self, run_id: str) -> AsyncIterator[AgentStreamEnvelope]: ...
    async def close(self, run_id: str, *, error: BaseException | None = None) -> None: ...
```

`publish` 必须在 `ReplyEnd` 之后发送终止哨兵，`subscribe` 必须保证 sequence
单调递增且只属于一个 `run_id`；订阅者提前断开时只取消自己的消费，不得悄悄
取消仍在运行的 Agent，除非调用方明确执行 `cancel()`。

### 阶段依赖与停止条件

| 阶段 | 前置条件 | 本阶段允许修改 | 进入下一阶段的硬门禁 |
|---|---|---|---|
| F36.1 | F35 已验收；当前运行 Gateway 不动 | 仅 PRD、矩阵、架构测试骨架 | 47 个 import、9 个 pyproject、事件生产/消费均有清单 |
| F36.2 | F30 `ftre-llm` 已验收 | `ftre-llm` 测试与 Runtime 过渡调用 | 两种 Adapter 等价；所有新调用走 `LlmService`；不删 Core ReAct |
| F36.3 | 公共消息/Hook/Tool 目标已冻结 | `ftre-agent` 契约和各消费者 import | 新类型 `__module__` 正确；Session/Inbox JSON round-trip 通过 |
| F36.4 | Tool 定义已迁入 `ftre-agent` | ToolService 私有索引、ToolView、权限/执行流水线 | 无 `ToolRegistry` Runtime 实例；scope/schema/permission/execute 一致 |
| F36.5 | LLM/Tool 公共契约稳定 | Runtime ReAct 算法、状态和真实 stream | 只有一个 Reason/Act/Exit；Retry/Cancel/Confirm/stream 回归通过 |
| F36.6 | 事件生产者/消费者矩阵已核对 | Host typed event、Desktop reducer/type | 新 Host event 可回放；删除事件无活跃生产/消费 |
| F36.7 | F36.2–F36.6 全部通过 | pyproject、wheel、旧 Core 依赖清理 | 洁净环境安装不需要 Core；Core C8 尚未完成时不能删外仓 |
| F36.8 | F36.7 扫描通过 | 清理残留、执行报告、PRD/TODO/CHANGELOG | 四文档一致，跨仓库 PR 门禁满足 |

任何硬门禁失败，停在当前阶段并修复；不得跳阶段、标记 done 或添加兼容别名
来掩盖失败。每一阶段的提交和测试输出必须能单独回放。

---

## 5. LLM Service 接口边界

`ftre-llm.LlmService` 的公开能力保持如下：

| 方法 | 入参 | 出参 | 归属 |
|---|---|---|---|
| `register_adapter` | provider 名称、Adapter Factory | 可逆 registration | LlmService |
| `list_providers` | 无 | `tuple[ProviderInfo, ...]` | LlmService |
| `resolve_model_info` | provider、model、api_type | `ModelInfo` | LlmService |
| `prepare_call` | `LlmCallConfig`、凭据 | `PreparedLlmCall` | LlmService |
| `stream` | `LlmRequest`，可选 `credentials`、`dispatch_stream_hooks` | `AsyncIterator[StreamChunk]` | LlmService |
| `close` | 无 | `None` | LlmService |

不加入：`retry()`、`fallback()`、`run_agent()`、`execute_tool()`、Session 状态。

示例：

```python
request = LlmRequest.from_parts(
    LlmCallConfig(
        provider="OpenCode 直连",
        model="glm-5.3-flash",
        api_type="completions",
        max_tokens=131072,
        reasoning_effort="high",
    ),
    messages=messages,
    tools=tools,
    purpose="conversation",
)

async for chunk in llm_service.stream(request):
    consume(chunk)
```

Retry 由 `ftre-agent-runtime` 的 ReasoningExecutor 执行，失败决策通过
`llm/error` Hook 交给 Recovery/Fallback Plugin。

### 5.1 Hook 触发时序（必须保持）

```text
ReasoningExecutor.attempt
  ├─ dispatch(llm/stream, LlmStreamPayload)
  │    └─ Hook 返回惰性 AsyncIterator；Fallback 只在最后一次零输出时替换
  ├─ consume StreamChunk
  ├─ FinishChunk(error/aborted) → LLMError
  └─ dispatch(llm/error, LLMErrorPayload)
       ├─ None / stop → TurnResult(error) → ReplyEnd(error)
       └─ retry → RetryEvent → backoff → 重新读取 Msg → 下一次 attempt
```

`llm/stream` 的 payload 只包含本次调用坐标、provider/model/purpose、只读
messages/tools、attempt、max_attempts、取消信号和一次性 `invoke()`；Plugin 不直接
持有 AgentState。`llm/error`
的 payload 只包含归一化错误码/消息和 attempt 坐标，不包含 API Key、原始异常对象
或完整 Prompt。两个 Hook 的失败策略必须是可观察而不覆盖原始错误；Listener 异常
不能生成第二次 Retry。

Hook 契约的字段必须按下表冻结，避免迁移时把凭据或可变状态泄漏给 Plugin：

| Hook | 入参关键字段 | 默认出参 | Plugin 允许改变的内容 |
|---|---|---|---|
| `llm/stream` | `agent_id/session_id/turn_id`、`provider/model/purpose`、只读 `messages/tools`、`attempt/max_attempts`、`cancellation`、`invoke` | `AsyncIterator[StreamChunk]` | 包裹或替换本次惰性流；不得修改消息、凭据或已提交输出 |
| `llm/error` | `agent_id/session_id/turn_id`、`iteration`、`model`、`error_code/error_message`、`attempt/max_attempts`、`cancellation` | `None`（交回 Runtime 默认分类） | `LLMErrorDecision(action=retry|stop, reason, delay)`；不得直接执行 Retry 或 fallback |

`LLMErrorPayload` 不携带 API Key、原始异常对象、完整 Prompt；`LlmCredentials` 只
在 `LlmService.prepare_call/stream(..., credentials=...)` 内部存在。`llm/stream`
的 fallback 必须满足“最后一次 attempt、尚无协议输出、错误可切换”，否则原错误
继续向 `llm/error` 传播。

### 5.2 LLM 适配器注册要求

```python
registration = llm.register_adapter("completions", OpenAICompletionsAdapter)
try:
    request = LlmRequest.from_parts(config, messages, tools, purpose="conversation")
    async for chunk in llm.stream(request):
        consume(chunk)
finally:
    registration.dispose()
```

`OpenAICompletionsAdapter` 和 `OpenAIResponsesAdapter` 只能在
`ftre_llm.adapters.plugin` 注册；Runtime、Compaction、Title 和 Recovery 不得
直接实例化它们。`prepare_call()` 每次创建 `max_retries=0` 的一次性句柄，失败
由 Runtime/Plugin 处理。

---

## 6. ToolService 接口边界

`ToolService` 公开能力保持如下：

| 方法 | 入参 | 出参 | 说明 |
|---|---|---|---|
| `register` | `ToolDefinition`、implementation、owner、scope、source | disposer | Plugin 贡献工具 |
| `restrict` | agent_id、owner、allow/deny | disposer | Agent 作用域限制 |
| `get` | name、agent_id | ToolContribution 或 None | 可见性查询 |
| `snapshot` | agent_id | ToolContribution 快照 | 不暴露内部 Registry |
| `schemas` | agent_id | OpenAI schema 列表 | 与 execute 共用投影 |
| `prepare_view` | agent/session/profile/llm 配置 | Runtime Tool View | 隔离视图 |
| `execute` | name、context、arguments、agent_id | 工具结果 | 作用域感知执行 |

Runtime 只依赖 `prepare_view()` 返回值和 `ToolDefinition` 公共定义，不读取 Service
私有字段。

### 6.1 ToolView 精确契约

```python
class ToolView(Protocol):
    @property
    def names(self) -> tuple[str, ...]: ...

    def schemas(self) -> tuple[ToolSchema, ...]: ...
    def get(self, name: str) -> ToolDefinition | None: ...

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolExecutionResult: ...
```

注册与执行的对象必须显式分开：

```python
@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: tuple[ToolParameter, ...]
    injected: tuple[str, ...] = ()
    execute: Callable[..., Any] | None = None

@dataclass(frozen=True, slots=True)
class ToolContribution:
    definition: ToolDefinition
    implementation: Callable[..., Any]
    owner: str
    source: str
    scope: str = "global"
```

上面的 `ToolDefinition` 位于 `ftre-agent.tool.definition`；`ToolContribution` 是
Host `src/ftre/services/tools/types.py` 的私有服务模型，不属于 Agent 公共契约。
`ToolService.register(contribution)` 只保存并索引 `ToolContribution`，并返回可逆
disposer；`ToolService.prepare_view()` 将可见的 `definition` 和一个受控的
`execute()` 端口投影给 Runtime。`execute` 只是 DSH 风格的 callable 契约，实际
调用仍由 ToolService 负责注入、权限、审批、取消、超时和结果归一化；Runtime
不得直接调用 `ToolDefinition.execute` 或读取 Service 私有索引。这样：

| 问题 | `ftre-agent.tool.definition` | `ToolService` |
|---|---|---|
| 工具名称/描述/参数 | 声明 | 读取并生成 schema |
| 工具是否对某 Agent 可见 | 不处理 | scope、allow/deny、shadow |
| 参数注入 | 只记录 `Injected` 标记 | 解析运行时 context |
| callable 执行 | 只声明 execute 契约 | 同步/异步调度、异常/取消归一化 |
| 生命周期 | 不持有资源 | owner/source、注册、卸载、MCP disposer |
| Runtime 访问 | 只读定义和 ToolView | 不暴露内部索引和 callable |

`ToolService.prepare_view()` 返回的是一次性、不可变可见性快照；View 中的
`execute()` 必须在执行前完成 `Injected` 参数解析、同步函数线程池调度、异步
函数 await、异常/取消归一化和 metadata 保留。Runtime `tool_calls.py` 只做：

1. 将模型 ToolCall 转成 `ToolExecutionInput`，调用 `executionMode()` 并分组；
2. 按模型顺序调用 View.execute（权限/审批/执行/归一化由 ToolService 完成）；
3. 并发 gather 但按原始 call 顺序提交 ToolResult 事件；
4. 取消时停止启动新的调用、排空已启动调用，并为未启动调用生成 cancelled 结果。

View 不得暴露底层注册表、MCP client、函数对象或 allow/deny 列表的可变引用。

---

## 7. 数据模型与事件简化原则

### 7.1 Agent 公共消息

```text
Msg
├─ id
├─ role: user | assistant | system
├─ name
├─ content: ContentBlock[]
├─ metadata
└─ token / created_at / finished_at
```

`Msg` 是 Session、Agent、Inbox、Compaction 共享的持久事实模型，因此放在
`ftre-agent`，不放入 SessionService 私有目录。

字段和不变量必须按当前 `ftre_agent_core.message._msg.Msg` 保持：

| 字段 | 类型/规则 | Owner |
|---|---|---|
| `id` | 16 位 hex；User/Assistant 边界不可复用 | ftre-agent |
| `role` | `user/assistant/system`；user 只能含 text/data，system 只能含 text | ftre-agent |
| `name` | `default/compact/compact_fast` 等语义标签，不承载 agent/model id | ftre-agent/Compaction |
| `content` | 判别联合 `Text/Thinking/Data/Hint/ToolCall/ToolResult` | ftre-agent |
| `metadata` | provider/session 扩展；Responses output item 只放这里 | Session/LLM wire |
| `token` | Assistant 的累计 usage + last_call_usage | SessionProjection |
| `created_at/finished_at` | ISO-8601；ReplyEnd 或用户边界封口 | SessionProjection |
| `finished_reason/error` | 结束原因和结构化错误；非结束态为空 | Runtime/Projection |

`Msg.append_event()` 必须继续支持 message_id 优先、reply_id 回退；同一 reply
中通过 UserMessageEvent 轮换 Assistant message_id 的行为不得改变。Session JSON
和 Inbox JSON 使用 `model_dump(mode="json")`，迁移后反序列化结果必须字节级兼容
（随机 id/timestamp 除外）。

### 7.1.1 Agent Service 公共数据模型

```text
AgentCreateSpec(agent_id, config, session_id?, metadata)
AgentResumeSpec(agent_id, session_id, config, checkpoint_id?, metadata)
AgentRunRequest(session_id, request_id, messages: tuple[Msg, ...], channel_id,
                source, metadata, options)
AgentRunResult(session_id, turn_id, status, user_message_id, final_content,
               usage?, error?, run_id)
AgentView(agent_id, state, session_id?, run_id?, created_at?, config_snapshot_hash?)
RunReservation(reservation_id, agent_id, session_id, request_id, expires_at)
```

`AgentRunRequest` 不出现 `InboundMessage`、`QueueItem`、Channel 实例或 Session
Repository；`AgentRunResult.status` 只允许 `completed/cancelled/failed`。Inbox
在自己的协议内把 `QueueItem` 转换为 Request，Agent Service 不反向读取 Inbox。

### 7.1.2 Runtime 私有数据模型

```text
Turn:
  turn_id, inbound: RuntimeInput, session_id, status,
  user_message_id, agent_profile, config, agent,
  messages, runtime_context, final_content, cancellation, confirm_event

RunState:
  status, iteration, done_reason, error/error_code,
  reply_id/message_id, empty_retries/in_finalization,
  token_usage, trace_span, cancel_token

TurnResult:
  text, reasoning, tool_calls: list[ToolCallRequest],
  finish_reason, usage, error: LLMError?
```

Turn 是 Host 编排状态，RunState 是单次 ReAct 算法状态；二者不能合并为一个
全局 AgentState，也不能由 AgentService 再复制一份。`AgentState` 只保留可恢复
的 `context: list[Msg]` 和 `permission_context`，在 Runtime 内部创建/销毁。

### 7.2 Agent StreamEvent

Agent StreamEvent 只表达实时 Agent 数据面：

```text
AgentStreamEvent
├─ reply lifecycle
├─ model call lifecycle
├─ text / thinking blocks
├─ tool call / tool result
├─ user confirmation
├─ retry
└─ inserted user message
```

Host Pipeline、Compaction、Session maintenance 不属于该联合类型。

### 7.2.1 Tool/LLM 公共值模型

```text
ToolDefinition(name, description, parameters, injected)
ToolParameter(name, type, description, required, enum?)
Injected(key)
ToolSchema(name, description, parameters, extra)
ToolContext(call_id, name, arguments, metadata, cancellation)
ToolExecutionResult(output, status, error?, metadata, value?)
ToolCallRequest(id, name, input: dict | None)

LlmCallConfig(provider, model, api_type?, reasoning_effort?, max_tokens?, timeout, metadata)
LlmRequest(config, messages, tools, purpose, session_id, turn_id, cancellation, attempt, max_attempts)
StreamChunk = BlockStart | *Delta | BlockEnd | Usage | Finish
```

`ToolContribution(definition, implementation, owner, source, scope)` 只存在于
Host ToolService；`implementation` 不属于公共 Agent 契约。`ToolCallRequest` 是
Runtime 执行模型；`ftre_llm.events.ToolCall` 在 F36.3 后
删除，不能同时保留两个名称相同语义的 Owner。`StreamChunk` 只由 `ftre-llm`
拥有，Agent Runtime 只消费它。

### 7.3 一个事件只保留一个表达方式

- 最大迭代：`ReplyEnd.finished_reason=exceed_max_iters`；
- 工具数据：统一进入 `ToolResultEnd.metadata` 或 ContentBlock，不保留无生产者
  的独立 DataDelta 事件；
- 用户确认输入：`UserConfirmResultEvent` 作为输入命令；
- Retry：只由 Runtime 产生，客户端读取 `retry` 事件；
- Usage：只由 `ModelCallEnd` 携带，不再新增独立 UsageEvent。

---

## 8. 测试与验收计划

### 8.1 单元测试

- `ftre-agent`：Msg、ContentBlock、Event、Hook、Tool 定义序列化；
- `ftre-agent-runtime`：ReAct 状态机、Reasoning、Acting、Exit、Retry、取消；
- `ftre-llm`：两种协议、流事件、usage、错误归一化；
- `ToolService`：注册、作用域、allow/deny、schema/execute 一致性。

**必须新增/迁移的回归用例**：

| 用例 | 断言 |
|---|---|
| 首轮纯文本 | `REPLY_START → MODEL_CALL_START → TEXT_* → MODEL_CALL_END → REPLY_END` 顺序稳定 |
| 多轮工具 | ToolCall 参数完整；ToolView 注入一次；ToolResult 与原 call_id 配对；下一轮能继续 |
| malformed tool JSON | 不执行工具，产 failed ToolResult，不让异常穿透整个 Run |
| LLM 失败可重试 | 每个 attempt 一个 `llm/error`；`RetryEvent.attempt` 单调；最多 `1 + max_retries` 次 |
| 不可重试错误 | 不触发 Retry；`AgentRunResult.failed` 和 `ReplyEnd(error)` 保留原始 code |
| Fallback | 前 N-1 次只 Retry；最后一次且无正文/思考/ToolCall/BlockStart 时才切备用；已有输出绝不拼接 |
| Cancel | `asyncio.CancelledError` 不进入 Retry/Fallback；所有 open block 和 ReplyEnd 正确收尾 |
| Tool ASK | `RequireUserConfirm` 不产 ReplyEnd；确认后从 Session context 恢复并只执行未完成 call |
| Steering | `UserMessageEvent` 先持久化；旧 Assistant 封口；新 Assistant message_id 生成且 reply_id 不变 |
| Runtime stream | `AgentService.stream` 收到实际 Text/Thinking/Tool/Model/Retry/ReplyEnd，不是单个伪事件 |
| Session attach | Gateway 重启后用 persisted Msg snapshot 恢复 open reply/ASK 状态，不依赖 Runtime 内存 |
| Tool scope | global/scoped shadow、allow/deny、卸载 disposer 后 get/schemas/execute 三者一致 |
| Protocol identity | 每个 HookSpec、Msg/Event/Tool 类型只存在一个 Python `__module__` Owner |

### 8.2 架构测试

- `ftre-agent` 不 import `ftre_agent_runtime`；
- `ftre-agent-runtime` 不 import `ftre.services` 私有实现；
- Runtime 不直接创建 Provider Adapter；
- Runtime 不访问 ToolService 私有 Registry；
- Host 不 import Core 旧路径；
- 每个 Service/Plugin 只有一个 Owner。

**扫描规则**：

```text
ftre-agent 的 contracts/message/event/tool 核心：只能依赖 stdlib/pydantic/ftre-llm
contracts，不得 import ftre Host/runtime；若保留 plugin.py，Cordis 依赖只能隔离
在 Provider 入口，不能被公共契约模块传递引入
ftre-agent-runtime：可以 import ftre-agent、ftre-llm；不得 import ftre.services.*
ftre-llm：不得 import ftre-agent-runtime、Session、ToolService、Inbox
ToolService：可以消费 ftre-agent.tool；不得暴露名为 ToolRegistry 的公共属性
Host Session/Inbox/Plugin：只能从 ftre-agent / ftre-llm 公共入口导入
```

AST 架构测试要覆盖 TYPE_CHECKING 分支和动态 import 字符串；不能只检查顶层
`from ... import`。扫描还要排除 `__pycache__`，并对活动测试单独执行一次。

### 8.3 集成测试

1. 创建 Agent → 注入 LLM Service → 准备 Tool View → 执行普通消息；
2. LLM 失败 → `llm/error` → Retry → Retry 耗尽 → Fallback；
3. Tool 调用 → Tool Hook → Tool Result → 下一轮 Reasoning；
4. Inbox Steer → UserMessageEvent → Assistant message_id 轮换；
5. Confirmation ASK → 恢复 → Tool 执行；
6. Compaction start/done/failed → Session Projection；
7. Gateway attach、断线恢复和关闭清理。

**跨仓库手动验证顺序**：

1. 在临时 `sessions_dir` 创建 Session，注入 fake LLM（一次成功、一次失败、
   一次 tool-call）和 fake Tool，运行 `AgentService.run()`；确认消息和事件顺序。
2. 让 fake LLM 在前两次返回 `internal_server_error`，第三次成功；确认日志有
   `llm/error`、两次 Retry、没有提前 fallback。
3. 让最后一次返回零输出错误；确认 fallback 只执行一次且只出现备用模型的完整流。
4. 在 ToolService 注册 global 与 scoped shadow，分别执行两个 Agent；卸载 scoped
   Plugin 后确认 global 工具仍可见，卸载 global 后确认无残留可执行工具。
5. 在 Desktop 测试页回放保留/删除事件；确认 `CUSTOM`、DataBlock 事件和
   `EXCEED_MAX_ITERS` 清理后，Reply/Usage/Retry/Confirm/UserMessage 仍可渲染。
6. 关闭 Composition（不操作正在运行的用户 Gateway），检查所有 Plugin effect、
   LLM PreparedCall、Tool disposer、Inbox worker、Trace exporter 均已释放。

### 8.4 质量门禁

```powershell
python -m pytest -q
python -m ruff check src tests packages --no-cache
git diff --check
python -m build --wheel --no-isolation
```

每个迁移阶段都必须执行对应专项测试；F36.7 必须执行全量测试、wheel 安装和
无 Core 依赖的洁净环境验证。

### 8.5 最终验收标准

- [x] AC1：所有 Core 公共契约已由 `ftre-agent` 提供，ftre 生产代码无旧 import。
- [x] AC2：所有 Agent 执行算法已由 `ftre-agent-runtime` 唯一拥有，没有第二套状态机。
- [x] AC3：`ftre-llm` 是唯一 LLM Adapter、StreamChunk、BlockAssembler 和 wire Owner。
- [x] AC4：ToolService 是唯一工具注册/作用域/schema/execute Owner。
- [x] AC5：Retry 只有一套循环，LlmService 没有隐藏重试。
- [x] AC6：删除事件清单已同步清理后端、客户端和测试，保留事件全部回归通过。
- [x] AC7：Session、Inbox、Compaction、Confirmation、Steer、Fallback 和取消行为不回归。
- [x] AC8：所有 Package wheel 构建成功，洁净环境安装不需要 `ftre-agent-core`。
- [x] AC9：`rg` 扫描生产代码中不存在 `ftre_agent_core`、旧 Registry、旧 Adapter 和兼容壳。
- [x] AC10：F36 PRD、TODO、CHANGELOG 和执行报告完成三联动；配对 Core C8 完成退休。
- [x] AC11：`AgentService` 状态只能由 Runtime 快照推进；不存在 AgentService 与
  AgentLoop 各自维护 active Task 的竞态，`is_busy/status` 在运行、暂停、压缩、
  取消和收尾期间一致。
- [x] AC12：`AgentService.stream()` 产生带 `run_id/sequence` 的真实
  `AgentStreamEnvelope`；客户端不再依赖刷新或伪造 `run.completed` 才能看到回复、
  token、Retry、Tool 和结束信息，同一 Run 不会被 `run()`/`stream()` 执行两次。
- [x] AC13：ToolView 是一次性不可变快照，ToolService 私有索引、MCP 连接和函数
  callable 不会通过 Runtime 或 Agent 公共 API 泄漏。
- [x] AC14：Responses reasoning item、DeepSeek reasoning_text、图片 content、
  tool_calls 和历史 Session JSON 在迁移前后行为等价，跨 Provider replay 不混用。
- [x] AC15：所有阶段均有独立测试、架构扫描、提交和执行记录；任何阶段失败会停在
  当前分支，不通过“兼容壳”绕过验收。

---

## 9. 提交与分支计划

每个切片单独提交，不在本地合并到 develop：

```text
feature/F36-agent-core-consolidation
├─ docs(prd): 冻结 Agent Core 合并边界
├─ refactor(llm): 收敛 LLM Adapter 到 ftre-llm
├─ refactor(agent): 迁移 Agent 公共契约
├─ refactor(tools): 解除 ToolService 对 Core Registry 的依赖
├─ refactor(agent-runtime): 合并 ReAct Runtime 到 Agent Package
├─ refactor(events): 删除无生产者事件并分离 Host 事件
├─ chore(packaging): 移除 ftre-agent-core 发行依赖
└─ docs(prd): 完成 Agent Core 合并审计
```

实际提交 scope 必须按 `docs/COMMIT.md` 和 `docs/TODO.yaml` 校验；如果提交类型
为 `feat/fix/prd/todos`，必须使用真实阶段 scope 并同步 PRD。每个 commit 只能
对应一个阶段切片；不得把 Core 删除、客户端改动和 ToolService 重写塞进同一个
“大迁移”提交。所有提交先推送 feature 分支，再通过 PR 合入 develop，禁止本地
merge。

---

## 10. 风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| 消息类型路径变化 | Session/Compaction/Inbox import 失败 | 先迁移公共契约，再迁移 Runtime；每阶段全量测试 |
| 两套 ReAct 状态机并存 | 行为分叉、Retry/取消不一致 | F36.5 强制唯一 Runtime Owner，架构测试扫描重复入口 |
| ToolService 与 Runtime Registry 边界不清 | 作用域绕过或生命周期泄漏 | 只允许 `prepare_view()` 返回隔离 View，禁止私有字段访问 |
| LLM Adapter 删除过早 | Runtime 启动失败 | F36.2 先完成 ftre-llm 集成和 wheel 验证，再删除 Core Adapter |
| 删除事件影响客户端 | reducer 丢事件或 UI 不更新 | F36.6 以生产者/消费者矩阵为删除门槛，后端和客户端同批验证 |
| 外部插件仍导入 Core | 用户插件加载失败 | 先扫描用户可见 API；迁移完成后发布变更说明，不保留运行时 alias |
| 运行中的 Gateway 受影响 | 当前任务中断 | 不重启现有后端，使用独立测试进程和 fake provider 验证 |
| `ftre-agent` 与 `ftre-llm` 循环依赖 | Package 无法独立安装 | HookSpec/Agent 契约只依赖 LLM contracts；`ftre-llm` 不反向 import Agent |
| `AgentService.stream()` 假事件被误认为真实流 | UI/调用方收不到增量事件 | F36.5 以真实事件队列或显式 EventSink 实现，并用回归测试断言顺序 |
| ToolView 泄漏函数/Registry | 作用域绕过、卸载残留、线程安全问题 | View 只暴露 names/schemas/get/execute；AST + 运行时隔离测试双重门禁 |
| Responses 历史 reasoning item 混用 | 上游 400 或思考内容丢失 | 保持 output item metadata；按目标 Provider 形状做组级 replay 校验 |
| Core C8 与 F36 不同步 | ftre wheel 仍能从旧 Core 偷加载 | F36.7 要求洁净环境安装，C8 提交 hash 写入执行报告后才发布 |

---

## 11. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-28 | 创建 F36 草稿：定义 Core 合并到 `ftre-agent`、`ftre-agent-runtime`、`ftre-llm` 和 ToolService 的分阶段方案；补充事件删除清单、接口、验收、提交和风险 | 独立 Core 已成为最后的发行边界债务；用户要求合并后进一步简化职责和事件面 |
| 2026-08-28 | 完成逐文件代码审查：确认 Runtime/AgentService 的三处状态、Runtime stream 只发伪 `run.completed`、LLM 已在 `ftre-llm` 但 Core 仍有重复实现、ToolService 仍依赖 Core Registry；重写阶段顺序、ToolView、Hook Owner、事件迁移和跨仓库门禁 | 防止按目录复制造成第二套状态机、第二套 Registry 或过早删除 Core 导致运行时回退 |
| 2026-08-28 | 二审扩展：加入 47 个引用文件的文件级迁移台账；补齐 `AgentService.stream()` 缺少 factory/reservation 与伪完成事件的修复要求；冻结 `llm/stream`、`llm/error` 字段、凭据隔离和 fallback 条件；修正 tracing 归属与提交 scope | 让执行 Agent 可按文件、调用点和硬门禁逐项交付，避免“目录搬完但流、状态、Hook 和提交规范仍不一致” |
| 2026-08-28 | 参考 DSH `ToolRuntime`、`dsh-agent-loop/tool-calls`、`dsh-user-approval` 和 `dsh-sandbox-policy` 复审权限边界：ToolService 拥有权限/审批/执行流水线，Runtime 仅保留 ToolCall 并发调度；Core `ToolHandler` 改为拆分迁移，不再整体落入 Runtime | 消除 ToolService、PermissionEngine、ToolHandler 三者职责重叠，明确可见性、审批、沙箱和 Agent 调度的 Owner |
| 2026-08-28 | 完成 F36 评审定稿：确认 DSH 风格的 ToolDefinition（含 execute 契约）、ToolService 权限/审批/执行流水线、Runtime `tool_calls.py` 调度边界，并冻结 Core 全量落点与 F36.1–F36.8 门禁 | 允许进入开发阶段；后续实现必须逐阶段对照本 PRD，不得再改变 Owner 方向 |
| 2026-08-28 | 完成 F36.3：消息、事件、Hook、ToolDefinition/ToolView/权限值模型迁入 `ftre-agent`；Host ToolService 改为内部 contribution + 一次性 ToolView，生产消费者改用公共 Tool 定义；新增公共契约门禁测试 | F36.3 验收通过；进入 F36.4，继续拆分 ToolService 执行流水线与 Runtime 调度 |
| 2026-08-28 | 完成 F36.4：ToolService 移除 Core ToolRegistry，改为内部 contribution 索引和不可变 ToolView；加入 Tool Hook、权限求值、审批端口、注入解析、异步/同步执行与结果归一化；MCP/内置 Tool Provider 改用 `ToolDefinition` | F36.4 验收通过；进入 F36.5，开始迁移 ReAct Runtime 与 ToolCall 调度 |
| 2026-08-28 | F36.1 基线完成：扫描确认 47 个 Core 引用文件、9 个直接依赖 Package，记录当前 Event producer/consumer、Desktop 残留和目标落点；`ftre-llm`/Runtime 专项验证 53 passed，ruff 通过 | 满足进入 F36.2 的基线门禁，后续迁移以这份清单为准 |
| 2026-08-28 | F36.2 验证完成：`ftre-llm` 与 Runtime 专项测试 53 passed，`ruff check packages/ftre-llm packages/ftre-agent-runtime` 通过；Runtime/Compaction/Title 已通过 `LlmService` 调用，未发现 Host/Runtime 直接实例化 Core Adapter | 现有 LLM Service、Completions/Responses Adapter、StreamChunk 和一次性 PreparedCall 已满足迁移前 Owner 门禁；Core LLM 副本延后到 F36.7 删除 |
| 2026-08-28 | F36.5/F36.6 完成：ReAct Reasoning→Acting→Exit、Retry/Cancel/Confirmation 与 ToolCall 调度全部归 `ftre-agent-runtime`；Agent 流和 Host `PIPELINE_EVENT`/`SESSION_MAINTENANCE` 分离，Desktop reducer/type 同步迁移；Ftre session/turn/hitl 专项 29 passed、全量 pytest 711 passed、Desktop 537 tests passed | 消除第二套状态机和混合事件流，确保实时/回放协议一致 |
| 2026-08-28 | F36.7 完成：删除 Ftre/Package 的 Core 依赖，构建 11 个 wheel 并检查 metadata 无 Core；Core C8 删除 `src/ftre_agent_core`、旧测试、示例和 `pyproject.toml`，不保留 alias/re-export；洁净 target 安装 11 个 wheel，所有新包可导入且 `ftre_agent_core` 不可发现 | 完成旧发行边界退休，保留 Core `docs/`、`work/` 和用户数据 |
| 2026-08-28 | F36.8 完成：执行跨仓残留/唯一 Owner/协议扫描，`node --check scripts/bundle-backend.js`、Ftre `ruff`、Desktop `pnpm test` 与 `git diff --check` 均通过；运行中的 Gateway 未被 kill 或重启；执行证据见 `docs/execution/EXECUTION-F36-agent-core-consolidation.md` | 形成可审计交付门禁，F36 与 Core C8 的 PR 边界保持独立 |
| 2026-08-28 | 收尾审计移除 `ToolService` 旧 `registry` 构造参数、`filter_tools(registry, ...)` 兼容入口及其旧 Registry 测试；allow/deny 统一由 `prepare_view()` 投影并通过 34 项 Agent/Tool 专项验证 | 清除最后一处重复可变 Registry Owner，避免以兼容入口掩盖职责重叠 |
| 2026-08-28 | Prompt 组装收口：Profile 的 SOUL/USER 与运行环境事实迁移到 `SystemPromptService`；删除 Runtime `factory.compose_system_prompt()`，Agent 创建只消费单次 `PromptAssembly.text`；补充 Profile、环境、vision 和 receipt 回归测试，全量 pytest 710 passed | 消除 Runtime 与 SystemPromptService 的双重组装，确保 Hook/Receipt 覆盖最终 system prompt 且不重复注入 |
| 2026-08-28 | 合并后 CI 修复：补齐 `ftre-agent` 的 `message/_block.py`、`_convert.py`、`_msg.py`，增加 `.gitignore` 精确例外；wheel 与 CI 同款测试（485 passed）通过 | 修复洁净安装缺少消息实现、导致全量测试收集失败的问题 |
| 2026-08-28 | 合并后 CI 第二次修复：补齐 `ftre-agent/event/_event.py` 并增加事件目录的 `.gitignore` 精确例外；wheel 与 CI 同款测试再次通过（485 passed） | 修复洁净安装缺少事件实现、导致事件包收集失败的问题 |
