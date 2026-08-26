# PRD-F31 Agent Runtime 依赖 Service 边界与契约收敛

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F31 |
| 名称 | Agent Runtime 依赖 Service 边界与契约收敛 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-25 |
| 定稿日期 | 2026-08-26 |
| 验收日期 | 2026-08-26 |
| 关联文档 | `docs/TODO.yaml` F31；`docs/prd/PRD-F30-llm-service-package.md`；`docs/prd/PRD-F14-final-plugin-first-architecture.md`；`AGENTS.md` |

---

## 1. 背景与目标

### 1.1 背景

ftre 当前已经建立了多个公开 Service：

```text
sessions
message_bus
tools
system_prompt
agent_profiles
hook_runtime
session_events
llm
```

但是 Agent Runtime 仍然直接持有或访问其中的具体实现：

```text
AgentLoop
├─ SessionService / SessionProjection
├─ EventBus / ChannelManager
├─ ToolRegistry / ToolService / MCP
├─ AgentManager / AgentConfig
├─ PromptAssembly Hook 内部类型
├─ WorkspaceAccessor
└─ create_llm_handler()
```

这会导致“已经存在 Service”却仍然无法独立移动 Agent Runtime：Runtime 依赖的是 Provider
实现和 Host 私有数据结构，而不是 Service 的稳定公开契约。

### 1.2 目标

本阶段不假设 Runtime 已经完成解耦，而是建立可执行的事实基线：记录 AgentLoop/TurnExecutor
当前每个具体依赖、确认 Service/Plugin/Hook 的唯一 Owner、冻结现有公开 API 的真实签名，
并用 Fake Service 契约测试和 AST 门禁保证债务不再增加。F32 依据本阶段输出完成真实的
Runtime 接线迁移；是否抽取 `ftre-agent` Package 另立阶段，不作为 F31 前置依赖。

### 1.3 非目标

- 不移动 AgentLoop、TurnExecutor、Driver 或 Provider 文件；
- 不创建 `packages/ftre-agent` 或 `packages/ftre-agent-runtime`；
- 不改变客户端 WebSocket/HTTP 协议；
- 不改变 Inbox、Queue、Steer、Command 或 Compaction 的业务语义；
- 不把 ChannelManager 变成 Agent 的新依赖；
- 不为每个类、Repository 或内部 helper 创建 Service；
- 不在本阶段实现新的 LLM Provider、Retry 或 Fallback 算法；
- 不修改 AgentLoop/TurnExecutor 的运行时构造和调用路径；
- 不新增 `AgentSessionView`、`AgentEventSink`、`AgentToolRuntime`、`PromptAssembler`、
  `AgentProfileResolver` 等平行 Protocol/DTO；
- 不保留新的 Facade、Port、Coordinator 或 Service Locator 作为临时层。

---

## 2. 边界原则

### 2.1 Service 和 Package 的区别

```text
Service  = 运行时通过 ctx 注入的能力
Package  = 安装、版本和发布边界
```

本阶段只冻结 Service 的公开边界，不进行 Package 移动，也不重接 Runtime。Service 内部可以
使用自己的 Provider/Repository；F32 的 Runtime 只能调用本节冻结的方法，不能越过 Service
读取其 `.manager`、`.registry`、`.projection` 或私有存储。

### 2.2 Agent Runtime 的唯一职责

```text
InboundMessage
    ↓
Agent Run
    ↓
Reasoning / Tool / LLM
    ↓
Assistant Event / AgentResult
```

Agent Runtime 只拥有 active Run、Turn 生命周期、取消、Hook 分发和运行结果；不拥有：

```text
pending / queue / claim / channel / command / session repository
```

### 2.3 F32 目标 Inject 图（F31 只冻结，不在本阶段实施）

```python
inject = (
    "llm",
    "sessions",
    "tools",
    "system_prompt",
    "agent_profiles",
    "message_bus",
    "hook_runtime",
    "session_events",
)
```

可选能力：

```python
"attachments"
"traces"
```

F32 必须从 Agent Runtime 依赖中移除：

```text
channels / ChannelManager
mcp / McpService
WorkspaceAccessor
ToolRegistry
AgentManager
SessionRepository
create_llm_handler
```

---

## 3. Service/Plugin/Hook Owner 与公开契约

本节冻结“当前真实 API + F32 迁移目标”，不新增平行 Protocol 或 DTO。F31 只记录和测试
契约；F32 才把 Runtime 的具体调用改到这些入口。每个 Service 的 Provider Plugin 仍是
唯一创建/销毁者，Repository、Manager、Registry 和 Projection 只在 Owner 内部使用。

### 3.1 Agent Service（`agents`）

Owner：`src/ftre/services/agent/plugin.py` 创建 `AgentService`，并在同一个 Plugin Fiber
中组装私有 `AgentLoop` 和 `AgentLoopDriver`。外部 Route、Channel、Inbox 和 Feature 只依赖
`AgentService` 的以下公开动作：

```python
await agents.run(inbound_message)
await agents.cancel(...)
agents.status(session_id)
agents.is_busy(session_id)
await agents.delete_session(session_id)
await agents.resume_confirmation(session_id, channel_id, events, metadata)
agents.list()
agents.get(agent_id)
agents.tool_scope(agent_id)
agents.scope_identity(agent_id)
agents.scope_carrier(agent_id, parent_id=None)
```

`AgentService.contracts.AgentDriver` 和 `AgentRegistryProtocol` 是当前已经存在的组合/测试
契约；F31 只记录它们，不新增同义 Protocol，也不把 `AgentLoop`、`TurnExecutor` 或 Driver
对象作为 Service API 暴露。`attach_driver()`、`detach_driver()` 和 `driver` 属性仅供
Agent Provider 的启动/关闭阶段使用，属于当前组合债务，F32 处理时不得再增加第二套绑定入口。

### 3.2 Session Service（`sessions`）

Owner：`src/ftre/services/session/plugin.py` 创建 `SessionService`，它拥有历史、消息、
Projection 和持久化。当前已存在并冻结的 Runtime 读写方法：

```python
await sessions.get_session(session_id)
await sessions.get_session_metadata(session_id)
await sessions.get_context_messages(session_id)
await sessions.save_message(session_id, message)
await sessions.update_message(message)
await sessions.upsert_message(session_id, message)
```

F32 需要补充一个窄的 `sessions.finish_open_replies(session_id, reason, error=None)`，
把当前 `sessions.projection.finish_open()` 的收尾逻辑收回 Owner；F31 不提前实现它。
Runtime 禁止使用 `sessions.projection`、`SessionRepository` 或直接读写状态文件。

### 3.3 MessageBus Service（`message_bus`）

Owner：`src/ftre/services/messaging/bus/plugin.py` 创建 `MessageBusService`。现有 inbound
方法是 `publish_inbound()`、`request_inbound()`、`stop_inbound()`；当前底层 `EventBus`
还提供 `publish_outbound(BusMessage)`，但 Runtime Provider 通过 `ctx.message_bus.bus`
越过了 Service。

F31 冻结的最小目标是由 `MessageBusService` 暴露同名 `publish_outbound()`，继续使用已有
`BusMessage`，不新造 `AgentEvent` 模型。Channel Plugin 负责把出站 BusMessage 转换成
WebSocket/Subagent/Cron 协议；F32 迁移后 Runtime 只调用 `message_bus.publish_outbound()`。

### 3.4 Tools Service（`tools`）

Owner：`src/ftre/services/tools/plugin.py` 创建 `ToolService`，只拥有工具贡献、权限过滤
和 Agent scoped view。MCP、Workspace、Attachment 各自仍是独立 Service/Plugin，不归并到
Tools。

当前真实公开方法：

```python
tools.schemas(agent_id)
tools.build_view(agent_id, session_id=None)  # 当前返回 Core ToolRegistry
tools.execute(name, execution_context=None, arguments=None)
```

F31 不新增 `AgentToolRuntime` 或 `cancel()` Protocol。F32 只需把 View 创建/权限过滤留在
Tools Owner，并在 Core 必须使用 `ToolRegistry` 的前提下由 `tools` 返回兼容 View；Runtime
不得访问 `tools.registry`、`McpService`、`WorkspaceAccessor`。

### 3.5 System Prompt Service（`system_prompt`）

Owner：`src/ftre/services/system_prompt/plugin.py` 创建 `SystemPromptService`。真实调用
契约是同步的 `assemble_result(agent_id, session_id, workspace, messages, base_prompt)`，
返回 `PromptAssembly`；`assemble()` 仅返回渲染文本。Runtime 不遍历 `_sections`、不构造
`PromptSection`，也不新增异步 `PromptAssembler`。

### 3.6 Agent Profile Service（`agent_profiles`）

Owner：`src/ftre/services/agent/profile/plugin.py` 创建 `AgentProfileService`，负责 Profile
文件、默认值和团队成员 Profile。真实 `resolve(agent_id, session_id=None)` 当前同步返回
`EffectiveProfile(agent_id, value)`；F31 冻结该入口并记录 `value` 形状过宽的债务，F32
只能消费快照，不能访问 `.manager`、`AgentManager.load()`、`load_config()` 或 Profile 目录。
F31 不另造 `EffectiveAgentProfile` 类型。

### 3.7 LLM Service（`llm`）

F30 已冻结 `ctx.llm.prepare_call()`、`PreparedLlmCall.stream()` 和 `ctx.llm.stream()`。
Agent Runtime 的 LLM 请求必须沿用 F30，不直接调用 `create_llm_handler()`；`agent/request`
由 `ftre-llm` 的 LlmService 发布，而不是由 Runtime 重复发布。

### 3.8 Hook Runtime（`hook_runtime`）

Owner：Composition 创建一个 `HookRuntime`。Runtime 只使用现有
`await hook_runtime.dispatch(spec, payload, context=...)`，Payload 来自当前 Host/Core
契约：`ftre.services.agent.hooks`、`ftre.services.system_prompt.hooks`、
`ftre_llm.contracts` 和 `ftre_agent_core.hooks`；不存在 `ftre-agent` 前置 Package。

### 3.9 Session Events（`session_events`）

Owner：`src/ftre/services/session/plugin.py` 创建 `SessionEventService`。真实入口是
`await session_events.emit(session_id, channel_id, event, metadata=...)`，它先调用 Session
Projection 再广播权威事实。Runtime 不直接调用 WebSocket，也不直接操作 Projection；F32
需要把当前 `session_projection.finish_open()` 收敛到 `sessions` 的公开收尾方法。

### 3.10 Owner 快照

| 能力 | 唯一 Owner | 当前公开入口 | F32 禁止越过的内部实现 |
|---|---|---|---|
| Agent Run/identity | `AgentService` / Agent Provider Plugin | `run/cancel/status/is_busy/delete_session/resume_confirmation`、registry scope 查询 | `AgentLoop`、`TurnExecutor`、`AgentLoopDriver`、`attach_driver` |
| Session 历史/消息 | `SessionService` | `get_context_messages/save_message/update_message/upsert_message` | `SessionRepository`、`.projection` |
| 出站传输 | `MessageBusService` | F32 补 `publish_outbound(BusMessage)` | `.bus`、ChannelManager |
| Tool View/权限 | `ToolService` | `schemas/build_view/execute` | `.registry`、MCP、Workspace |
| Prompt | `SystemPromptService` | `assemble_result` | `_sections`、PromptSection |
| Profile | `AgentProfileService` | `resolve` | `.manager`、Profile 目录 |
| LLM | `LlmService` | `prepare_call/stream` | `create_llm_handler` |
| Hook | `HookRuntime` | `dispatch` | 第二 Dispatcher/Locator |
| Session 事件 | `SessionEventService` | `emit` | Projection、WebSocket |

---

## 4. Agent Runtime 改造边界

### 4.1 当前真实构造参数（F31 基线）

`src/ftre/services/agent/runtime/engine.py:60-78` 的 `AgentLoop` 当前直接接收：

```text
bus / session_manager / channel_manager / config / event_hub
tool_registry / tool_service / mcp_service / agent_manager / agent_registry
agent_service / attachments / system_prompt / hook_runtime / traces / session_events / llm_service
```

`src/ftre/services/agent/runtime/provider.py:26-58` 还把 `ctx.message_bus.bus`、
`ctx.channels.manager`、`ctx.tools.registry` 和 `ctx.agent_profiles.manager` 传入 Runtime。
这些是 F31 要冻结的债务证据，不在 F31 中删除。

### 4.2 F32 的迁移输入（F31 只冻结）

F32 才修改构造和调用点。目标仍使用现有 `AgentLoop` 私有 Runtime，不改名、不创建新的
公共 `AgentRuntime` 类：

```text
sessions / message_bus / tools / system_prompt / agent_profiles / llm
hook_runtime / session_events / optional attachments / optional traces
```

F32 必须删除 Runtime 构造参数和字段：

```text
channel_manager / mcp_service / tool_registry / agent_manager / session_projection
```

F31 的交付物是“当前参数 → 上述目标依赖”的逐项映射和阻塞说明，而不是提前改造构造函数。

### 4.3 Channel 与出站事件边界

当前 Runtime 仍通过底层 `EventBus.publish_outbound(BusMessage)` 发送状态和 Agent 事件；
Channel Plugin 再将 BusMessage 转成 WebSocket/Subagent/Cron 协议。F32 通过
`MessageBusService.publish_outbound(BusMessage)` 收口调用，不新增 `AgentEvent` DTO，
也不让 Runtime 直接持有 ChannelManager。

### 4.4 输入边界

公开入口 `AgentLoop.run_inbound()` 已接收 `InboundMessage`，但当前会在
`engine.py:259-276` 转换成 `BusMessage`，再交给 `TurnExecutor.execute()`，且
`Turn.inbound` 仍声明为 `BusMessage`。F32 才把转换限制在入口适配处并让 Turn 使用稳定的
输入快照；QueueItem、pending、claim、mode 和客户端队列状态仍只属于 Inbox Package。

---

## 5. Hook 迁移边界

F31 只收敛依赖和契约，不新增业务 Hook。目标 Hook 如下：

```text
agent/before-run       # active Run 开始前的准入
agent/request          # 每次 LLM 请求前的配置提议
llm/error              # 单次 LLM attempt 失败后的 retry/stop 裁决（由 Core 发布）
agent/stop-decision    # Agent 准备停止当前 Turn 前
agent/after-run        # active Run 完成后的收尾
```

Hook 的 Owner：

| Hook | 发布者 | 监听者 | 返回值 |
|---|---|---|---|
| `agent/before-run` | Agent Runtime | 权限/治理 Plugin | `AllowRun` / `RejectRun` |
| `agent/request` | `ftre-llm.LlmService` | 模型选择/Fallback Plugin | `LlmCallConfig` |
| `llm/error` | Agent Core | Retry/恢复策略 Plugin | `LLMErrorDecision` / `None` |
| `agent/stop-decision` | Agent Core | Steering/Continuation Plugin | `StopTurn` / `ContinueTurn` |
| `agent/after-run` | Agent Runtime | Compaction/维护 Plugin | `None` |
| `llm/stream` | `ftre-llm`/Agent Core | 计量、Fallback、包装 Plugin | `AsyncIterator[StreamChunk]` |

F31 禁止 Runtime 直接 import 某个监听 Plugin 的实现。

---

## 6. 迁移映射

| 当前路径/符号 | 当前事实证据 | F31 冻结的公开边界 | F32 精确处理 |
|---|---|---|---|
| `agents` / `AgentService` | `services/agent/plugin.py`、`service.py`、`contracts.py` | `run/cancel/status/is_busy/delete_session/resume_confirmation` 和 registry scope 查询；现有 `AgentDriver` 仅作组合契约 | 保留单一 `AgentService` Owner；不新增 Runtime Service 或第二 Driver 绑定 |
| `session_manager` / `SessionService` | `engine.py:81`、`turn_executor.py:268` | `get_session/get_context_messages/save_message/update_message/upsert_message` | 删除 `.projection` 绕过，补 `finish_open_replies` |
| `session_projection` / `SessionProjection` | `engine.py:130`、`turn_executor.py:624` | Session Service 内部状态 | 迁移到 `sessions.finish_open_replies()` |
| `bus` / `EventBus` | `provider.py:29`、`engine.py:586` | `message_bus` 出站方法 | MessageBusService 补 `publish_outbound()` |
| `channel_manager` / `ChannelManager` | `provider.py:33`、Core Agent 构造参数 | 不属于 Runtime | 从 Provider、Loop、Core 创建参数删除 |
| `tool_registry` / Core `ToolRegistry` | `provider.py:37`、`engine.py:16` | Tools Service 创建的 Core Tool View | Runtime 不访问 `.registry`；保留 Core 必需对象 |
| `tool_service` / `ToolService` | `engine.py:142-147` | `schemas/build_view/execute` | 由 Tools Owner 完成 View 和权限过滤 |
| `mcp_service` / MCP Plugin | `provider.py:40`、`engine.py:141-145` | MCP 独立 Plugin，不进入 Runtime | 删除 Runtime 直接准备 MCP |
| `WorkspaceAccessor` | `turn_executor.py:45,442` | Workspace Service/Tool Context | Runtime 不构造 WorkspaceAccessor |
| `agent_manager` / `AgentManager` | `provider.py:42`、`turn_executor.py:377,428,816` | `agent_profiles.resolve()` | Runtime 私有工厂消费 Profile 快照 |
| `AgentConfig` / `load_config()` | `turn_executor.py:32,737` | 配置/Profile Service 快照 | 删除 Runtime 直接读全局配置 |
| `system_prompt` | `turn_executor.py:640-675` | `assemble_result()` | 保持同步真实 API，不新增 Prompt Protocol |
| `llm_service` / Core LLM Handler | `turn_executor.py:382-430` | F30 `prepare_call/stream` | 删除 `create_llm_handler` 直连 |
| `hook_runtime` | `engine.py:95-171` | 现有 `dispatch(spec,payload,context=...)` | 不创建第二 Dispatcher |
| `session_events` | `engine.py:520-526` | `emit(session_id,channel_id,event,metadata=...)` | 收敛 Session 事实广播，不重复出站 |
| `InboundMessage → BusMessage` | `engine.py:259-276`、`Turn.inbound` | `InboundMessage` 输入快照 | F32 删除 Turn 内 BusMessage |

---

## 7. 非功能需求

- **单一 Owner**：每个 Service key 只有一个 Provider；Agent Runtime 不创建 Service。
- **无 Service Locator**：`AgentLoop`/`TurnExecutor` 不使用 `ctx.get()` 查找依赖；Provider
  可以在声明了 optional 依赖后解析 `attachments`/`traces`。
- **无新增具体跨 Owner import**：F31 不为 Runtime 增加 Host Repository、Manager、Channel
  或 Plugin 私有实现依赖。当前 `turn_executor.py` 的 `WorkspaceAccessor`、Profile Manager
  类型检查导入以及 Core `ToolRegistry` 是第 6 节明确登记的存量债务，由 F32 删除或收敛；
  Core 算法类型属于明确登记的跨仓集成依赖。
- **可卸载**：Service 注册、Hook、事件订阅、任务和资源绑定 Fiber；卸载后不得继续影响 Run。
- **可测试**：契约测试用测试内 Fake Service/duck typing 验证调用形状，不在生产代码新增
  `*Port` 或平行 Protocol；F31 不要求 Fake 驱动完整 Agent Run。
- **不改变数据协议**：F31 不改变现有 Session JSON、Queue wire 或客户端消息。
- **不增加中间层**：若已有 Service 能提供窄接口，直接扩展其公开方法，不新增同义 Port；
  当前已知债务必须登记在迁移矩阵，门禁只保证数量不增加。

---

## 8. 验收标准

- [x] **AC1**：依赖矩阵覆盖 AgentLoop、TurnExecutor、Provider 的真实 import、属性读取、Service
  key、Owner、当前债务和 F32 迁移目标。
- [x] **AC2**：冻结现有 `sessions` 公开方法和 F32 所需 `finish_open_replies` 缺口；Repository、
  Projection 直达点全部有路径证据和删除批次。
- [x] **AC3**：冻结 `message_bus` 的实际 inbound/outbound 边界，不虚构 `AgentEvent`；记录
  `MessageBusService.publish_outbound()` 的 F32 补齐项和 ChannelManager 删除点。
- [x] **AC4**：冻结 `tools.schemas/build_view/execute` 真实入口，明确 Core `ToolRegistry` 是已登记
  的集成依赖；MCP、Workspace、Attachment 仍由各自 Owner 管理。
- [x] **AC5**：冻结同步 `system_prompt.assemble_result()` 和 `PromptAssembly` 返回形状，不新增
  `PromptAssembler` Protocol。
- [x] **AC6**：冻结 `agent_profiles.resolve()` 当前 `EffectiveProfile` 形状，登记 `.manager`、
  `AgentManager` 和配置目录的 F32 清理点，不新增 `EffectiveAgentProfile`。
- [x] **AC7**：确认 Runtime 的 LLM 请求遵循 F30 `prepare_call/stream()`，并记录当前 Adapter 注入
  和 Core Runner 边界。
- [x] **AC8**：Hook 发布者、监听者、模式和失败语义有契约测试，未新增重复 Hook、Port、Facade
  或 Coordinator。
- [x] **AC9**：AST 门禁阻止新的 Host concrete import、Service Locator、Channel 反向依赖和跨
  Owner private import，同时允许并精确锁定 F31 基线债务集合。
- [x] **AC10**：现有普通消息、Tool、Steer、Compaction、Retry、Fallback、Confirmation、取消、
  Session 恢复和客户端协议回归通过。
- [x] **AC11**：未移动 AgentLoop/TurnExecutor、未创建 Agent Package、未修改 Client/Inbox/Queue/
  Session wire；本阶段只提交边界基线和测试门禁。
- [x] **AC12**：执行报告、F31 迁移矩阵、TODO 和变更记录与真实代码、测试和已知债务一致。

---

## 9. 测试计划

### 9.1 契约测试

- SessionService 当前公开读写方法和 F32 收尾缺口的测试内 Fake；
- MessageBusService 当前 inbound 方法与 F32 outbound 出口形状；
- ToolService `schemas/build_view/execute` 及 Core View 兼容边界；
- SystemPromptService `assemble_result()` 输入输出；
- AgentProfileService `resolve()` 与 `EffectiveProfile` 快照；
- Hook Dispatcher 真实 payload/result、发布者和失败语义校验。

### 9.2 架构扫描

- 记录并锁定当前 `ChannelManager`、`McpService`、`ToolRegistry`、`AgentManager`、
  `WorkspaceAccessor`、`SessionProjection` concrete 依赖基线；新增依赖必须失败；
- `TurnExecutor` 不新增 `create_llm_handler()` 或 Core 协议桥；
- Runtime 不新增 Session Repository/Projection 私有 import；现有 Workspace/Profile Manager
  类型检查导入按矩阵登记，F32 删除时再收紧为零；
- Runtime 不通过 `ctx.get()` 查找依赖，Provider 的 optional 解析必须有声明；
- Package/Service 没有重复 Provider 或重复 HookSpec；
- 所有 F31 基线债务都在迁移矩阵中有 F32 owner 和删除条件。

### 9.3 回归测试

- 普通 InboundMessage 执行；
- Steer 在下一次 Reasoning 注入；
- Tool Call 和 Tool Result；
- Compaction after-run；
- Retry/Fallback；
- Session 恢复和确认流程；
- 取消、unload、restart、in-flight Hook。

---

## 10. 实施批次

| 批次 | 内容 | 产物 |
|---|---|---|
| F31.1 | 真实依赖矩阵与 Owner 基线 | `docs/execution/matrices/F31-agent-service-boundaries.md`、AST 基线 |
| F31.2 | Session/MessageBus 真实公开 API 冻结 | 方法表、F32 缺口和出站边界 |
| F31.3 | Tools/Prompt/Profile 真实公开 API 冻结 | 方法表、Core 集成例外和 Owner 表 |
| F31.4 | LLM/Hook Owner 与失败语义确认 | Hook 矩阵、F30 交叉引用和边界测试 |
| F31.5 | Fake Service 契约与 AST 不增债门禁 | 测试内 Fake、架构扫描、回归基线 |
| F31.6 | 文档验收 | 执行报告、TODO、CHANGELOG、变更记录 |

F31 完成后才进入 F32。F32 才允许修改 AgentLoop/TurnExecutor 的构造和调用实现。

---

## 11. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-25 | 初始草案：先收敛 Agent Runtime 依赖的 Service 边界，再进行 Agent Runtime Package 化 | Agent Runtime 当前依赖多个 Service 的具体 Provider/Repository/Manager，直接移动会把架构债务带入新 Package |
| 2026-08-26 | F31 定稿收窄为真实依赖矩阵、Service/Plugin/Hook Owner、公开 API 契约、Fake/AST 门禁和 F32 迁移基线；删除虚构 Protocol/DTO、错误 Owner、`ftre-agent` 前置依赖及运行时迁移承诺 | 代码审查发现运行时迁移属于 F32，F31 只应冻结事实和防止债务继续增加 |
