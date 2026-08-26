# PRD-F31 Agent Runtime 依赖 Service 边界与契约收敛

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F31 |
| 名称 | Agent Runtime 依赖 Service 边界与契约收敛 |
| 状态 | 草稿 |
| 创建日期 | 2026-08-25 |
| 定稿日期 | 待评审 |
| 验收日期 | 待开发 |
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

本阶段完成后，AgentLoop 和 TurnExecutor 的依赖图只通过公开 Service API 和 Agent Hook
契约连接；每个 Service 的唯一 Owner、输入、输出、生命周期和错误语义明确，为下一阶段
F32 Agent Runtime 解耦以及 F33 `ftre-agent` Package 抽取建立事实基线。

### 1.3 非目标

- 不移动 AgentLoop、TurnExecutor、Driver 或 Provider 文件；
- 不创建 `packages/ftre-agent` 或 `packages/ftre-agent-runtime`；
- 不改变客户端 WebSocket/HTTP 协议；
- 不改变 Inbox、Queue、Steer、Command 或 Compaction 的业务语义；
- 不把 ChannelManager 变成 Agent 的新依赖；
- 不为每个类、Repository 或内部 helper 创建 Service；
- 不在本阶段实现新的 LLM Provider、Retry 或 Fallback 算法；
- 不保留新的 Facade、Port、Coordinator 或 Service Locator 作为临时层。

---

## 2. 边界原则

### 2.1 Service 和 Package 的区别

```text
Service  = 运行时通过 ctx 注入的能力
Package  = 安装、版本和发布边界
```

本阶段只收敛 Service，不进行 Package 移动。Agent Runtime 可以依赖多个公开 Service，
但不得依赖这些 Service 的具体 Provider、Repository 或 Runtime 私有实现。

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

### 2.3 目标 Inject 图

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

必须从 Agent Runtime 依赖中移除：

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

## 3. Service Owner 与公开契约

本节是 F31 的核心契约。开发时如果当前 Service 实现缺少方法，应补齐公开方法；不得让
Agent Runtime 通过 `ctx.get()`、`.manager`、`.registry` 或私有属性绕过契约。

### 3.1 Session Service（`sessions`）

#### Owner

`SessionService` 是 Session 历史、Projection、持久化和 Session Event 的唯一 Owner。
Repository、Projection、JSON/SQLite 存储只能在 Session Service 内部使用。

#### Agent Runtime 允许使用的接口

```python
class AgentSessionView(Protocol):
    async def load_context(self, session_id: str) -> SessionContext: ...

    async def append_user_message(
        self,
        session_id: str,
        message: UserMessageInput,
    ) -> str: ...

    async def append_assistant_message(
        self,
        session_id: str,
        message: AssistantMessageInput,
    ) -> str: ...

    async def append_event(
        self,
        session_id: str,
        event: SessionEventInput,
    ) -> None: ...

    async def checkpoint(
        self,
        session_id: str,
        turn_id: str,
    ) -> None: ...
```

#### 禁止

```python
sessions.repository
sessions.projection
SessionRepository(...)
```

Agent Runtime 只能通过 `sessions` Service 完成历史读取、消息写入和 checkpoint，不得自己
构造 Projection 或直接修改 Session 状态文件。

### 3.2 MessageBus Service（`message_bus`）

#### Owner

`MessageBusService` 是进程内 AgentEvent 传输的 Owner；它只负责发布/订阅，不负责持久化、
Queue admission 或 WebSocket 协议。

#### Agent Runtime 允许使用的接口

```python
class AgentEventSink(Protocol):
    async def publish(self, event: AgentEvent) -> None: ...

    def subscribe(
        self,
        event_type: str,
        listener: Callable[[AgentEvent], Awaitable[None]],
    ) -> Callable[[], bool]: ...
```

推荐的 AgentEvent：

```python
@dataclass(frozen=True, slots=True)
class AgentEvent:
    event_type: str
    session_id: str
    run_id: str
    turn_id: str
    payload: Mapping[str, Any]
```

#### 禁止

```python
channel_manager.send(...)
BusMessage 作为 Agent Runtime 的内部状态模型
```

Channel Plugin 订阅 MessageBus 并转换成 WebSocket/Subagent/Cron 协议；Agent Runtime 不知道
Channel 的存在。

### 3.3 Tools Service（`tools`）

#### Owner

`ToolsService` 是工具定义、权限、执行、取消、MCP 工具适配和工具结果归一化的公共 Owner。
MCP、Workspace 和工具附件能力由 Tools/Attachment Service 吸收，Agent Runtime 不直接使用。

#### Agent Runtime 允许使用的接口

```python
class AgentToolRuntime(Protocol):
    async def schemas(
        self,
        agent_id: str,
        scope: str | None = None,
    ) -> tuple[ToolSchema, ...]: ...

    async def execute(
        self,
        call: ToolCallInput,
        context: ToolExecutionContext,
    ) -> ToolResult: ...

    async def cancel(self, call_id: str) -> bool: ...
```

#### 禁止

```python
tools.registry
ToolRegistry(...)
ctx.get("mcp")
WorkspaceAccessor(...)
```

Agent Runtime 只提交 `ToolCallInput`，不负责工具注册表、MCP 连接、Workspace 路径和权限
细节。

### 3.4 System Prompt Service（`system_prompt`）

#### Owner

`SystemPromptService` 是结构化 Prompt Section 的组装 Owner。Context Govern、Skill、Plan、
MCP 等 Plugin 通过 Prompt Hook 贡献 Section。

#### Agent Runtime 允许使用的接口

```python
class PromptAssembler(Protocol):
    async def assemble(
        self,
        request: PromptAssemblyRequest,
    ) -> PromptAssembly:
        ...
```

```python
@dataclass(frozen=True, slots=True)
class PromptAssemblyRequest:
    agent_id: str
    session_id: str
    turn_id: str
    workspace: str | None = None
    purpose: str = "conversation"
```

Agent Runtime 只拿到最终 PromptAssembly，不直接构造或遍历 Prompt Section 内部对象。

### 3.5 Agent Profile Service（`agent_profiles`）

#### Owner

`AgentProfileService` 是 Agent 配置、Profile 文件、默认值、Team 成员 Profile 和配置校验的
唯一 Owner。

#### Agent Runtime 允许使用的接口

```python
class AgentProfileResolver(Protocol):
    async def resolve(
        self,
        agent_id: str,
        session_id: str | None = None,
    ) -> EffectiveAgentProfile: ...
```

```python
@dataclass(frozen=True, slots=True)
class EffectiveAgentProfile:
    agent_id: str
    provider: str
    model: str
    reasoning_effort: str | None
    tool_names: tuple[str, ...]
    system_prompt_id: str | None
    workspace_policy: Mapping[str, Any]
```

#### 禁止

```python
ctx.agent_profiles.manager
AgentManager.load(...)
load_config()
直接读取 ~/.ftre/agents/
```

### 3.6 LLM Service（`llm`）

F31 只定义 Agent Runtime 的消费边界；完整 Service 由 F30 PRD 实现。

```python
prepared = await ctx.llm.prepare_call(call_config)
async for chunk in prepared.stream(request):
    ...
```

Agent Runtime 不得直接调用：

```python
create_llm_handler(...)
```

### 3.7 Hook Runtime（`hook_runtime`）

#### Agent Runtime 允许使用的接口

```python
class HookDispatcher(Protocol):
    async def dispatch(
        self,
        spec: HookSpec,
        payload: Any,
        *,
        context: object | None = None,
    ) -> Any: ...
```

Agent Hook 的公开 Payload 必须来自 `ftre-agent` 契约，不得从 Agent Runtime 私有模块导出。

### 3.8 Session Events（`session_events`）

`session_events` 负责向 Session 生命周期和客户端投影发布规范化事件。Agent Runtime 只调用：

```python
await session_events.publish(
    SessionRuntimeEvent(
        session_id=session_id,
        run_id=run_id,
        event_type="agent/run-status",
        payload={"status": "running"},
    )
)
```

它不直接调用 WebSocket，也不直接写 JSON 状态快照。

---

## 4. Agent Runtime 改造边界

### 4.1 当前构造参数

当前 `AgentLoop` 直接接收：

```text
bus
session_manager
channel_manager
config
event_hub
tool_registry
tool_service
mcp_service
agent_manager
agent_registry
agent_service
attachments
system_prompt
hook_runtime
traces
session_events
```

### 4.2 F31 目标构造参数

F31 只要求契约整理和调用点迁移准备；F32 才修改 Runtime 构造，但目标必须冻结为：

```python
AgentRuntime(
    sessions=ctx.sessions,
    events=ctx.message_bus,
    tools=ctx.tools,
    prompts=ctx.system_prompt,
    profiles=ctx.agent_profiles,
    llm=ctx.llm,
    hooks=ctx.hook_runtime,
    session_events=ctx.session_events,
    attachments=ctx.get("attachments", strict=False),
    traces=ctx.get("traces", strict=False),
)
```

最终不再传入：

```python
channel_manager
tool_registry
mcp_service
agent_manager
WorkspaceAccessor
```

### 4.3 Channel 解耦

```text
Agent Runtime
    ↓ AgentEvent
MessageBus Service
    ↓
Channel Plugin
    ↓
WebSocket / Subagent / Cron
```

Agent Runtime 不直接发送 Channel 消息。Channel 变化不应导致 Agent Runtime Package 重新发布。

### 4.4 输入边界

Agent Runtime 接收稳定的 `InboundMessage`，不再让 `BusMessage` 进入 Turn 状态：

```python
@dataclass(frozen=True, slots=True)
class InboundMessage:
    session_id: str
    request_id: str
    channel_id: str
    content: str
    attachments: tuple[AttachmentRef, ...] = ()
    source: str = "user"
    metadata: Mapping[str, Any] = field(default_factory=dict)
```

Queue Item、pending、claim、mode 和客户端队列状态在 Inbox Package 内部完成转换。

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
| `agent/request` | Agent Runtime | 模型选择/Fallback Plugin | `LlmCallConfig` |
| `llm/error` | Agent Core | Retry/恢复策略 Plugin | `LLMErrorDecision` / `None` |
| `agent/stop-decision` | Agent Core/Runtime | Steering/Continuation Plugin | `StopTurn` / `ContinueTurn` |
| `agent/after-run` | Agent Runtime | Compaction/维护 Plugin | `None` |

F31 禁止 Runtime 直接 import 某个监听 Plugin 的实现。

---

## 6. 迁移映射

| 当前依赖 | F31 目标公开边界 | F32 处理 |
|---|---|---|
| `SessionService` 具体方法 | `sessions` AgentSessionView | 改调用点 |
| `SessionProjection` | `sessions.load_context/checkpoint` | 删除 Runtime 直接访问 |
| `EventBus` | `message_bus.publish` | 改为 AgentEvent |
| `ChannelManager` | 不再作为 Agent 依赖 | 从 Runtime 删除 |
| `ToolRegistry` | `tools.schemas/execute/cancel` | 改调用点 |
| `McpService` | Tools Service 内部 | 从 Runtime 删除 |
| `WorkspaceAccessor` | Tools/Workspace Service 内部 | 从 Runtime 删除 |
| `AgentManager` | `agent_profiles.resolve` | 改调用点 |
| `AgentConfig` | `EffectiveAgentProfile` | 由 Profile Service 解析 |
| `PromptAssembly` 内部对象 | `system_prompt.assemble` | 只消费最终结果 |
| `create_llm_handler` | `llm.prepare_call/stream` | 删除直接调用 |
| `HookRuntime` 具体实现 | HookDispatcher Protocol | Package 化时稳定 |
| `attachments` | Attachment Service Protocol | 保留可选注入 |
| `traces` | Trace Service Protocol | 保留可选注入 |

---

## 7. 非功能需求

- **单一 Owner**：每个 Service key 只有一个 Provider；Agent Runtime 不创建 Service。
- **无 Service Locator**：禁止 `ctx.get()` 作为 Agent Runtime 的隐式必选依赖。
- **无具体跨 Owner import**：Runtime 不 import Repository、Manager、Registry、Channel 或
  Plugin 私有实现。
- **可卸载**：Service 注册、Hook、事件订阅、任务和资源绑定 Fiber；卸载后不得继续影响 Run。
- **可测试**：Agent Runtime 可以用 Fake Service Protocol 运行，不需要真实 Session、Channel、
  MCP 或文件系统。
- **不改变数据协议**：F31 不改变现有 Session JSON、Queue wire 或客户端消息。
- **不增加中间层**：若已有 Service 能提供窄接口，直接扩展其公开方法，不新增同义 Port。

---

## 8. 验收标准

- [ ] **AC1**：完成 AgentLoop/TurnExecutor 的完整依赖矩阵，列出每个具体 import、Service key、
  Owner 和迁移目标。
- [ ] **AC2**：`sessions` 提供 Agent Runtime 所需的历史、消息、事件和 checkpoint 公开方法；
  Runtime 不直接访问 Repository/Projection。
- [ ] **AC3**：`message_bus` 提供 AgentEvent 发布契约；Agent Runtime 不直接依赖 ChannelManager。
- [ ] **AC4**：`tools` 提供 schemas/execute/cancel；Runtime 不直接依赖 ToolRegistry、MCP 或
  Workspace 具体实现。
- [ ] **AC5**：`system_prompt` 提供 `assemble()`；Runtime 不直接构造 PromptAssembly 内部对象。
- [ ] **AC6**：`agent_profiles` 提供 `resolve()`；Runtime 不直接读取 AgentManager、AgentConfig
  或配置目录。
- [ ] **AC7**：Agent Runtime 的 LLM 调用全部经过 F30 `ctx.llm.prepare_call/stream()`。
- [ ] **AC8**：Agent Hook Payload 与 Dispatcher 契约可被独立 Fake Service 测试，未引入新的重复
  Hook、Port、Facade 或 Coordinator。
- [ ] **AC9**：AST 架构门禁拒绝 concrete import、Service Locator、Channel 反向依赖和跨 Owner
  private import。
- [ ] **AC10**：现有普通消息、Tool、Steer、Compaction、Retry、Fallback 和客户端协议回归通过。
- [ ] **AC11**：未移动 AgentLoop/TurnExecutor，未创建 Agent Package；本阶段只完成 Service 边界
  和调用契约。
- [ ] **AC12**：更新执行报告、迁移矩阵和 TODO；文档状态与实际代码一致。

---

## 9. 测试计划

### 9.1 契约测试

- Session AgentSessionView 的读写和 checkpoint；
- MessageBus AgentEvent 发布和订阅清理；
- Tools schemas/execute/cancel；
- Prompt `assemble()` 输入输出；
- Profile `resolve()` 输入输出；
- Hook Dispatcher payload/result 类型校验。

### 9.2 架构扫描

- `AgentLoop` 不 import `ChannelManager`、`McpService`、`ToolRegistry`、`AgentManager`；
- `TurnExecutor` 不调用 `create_llm_handler()`；
- Agent Runtime 不 import Session Repository/Projection 私有模块；
- Agent Runtime 不通过 `ctx.get()` 静默获取必选 Service；
- Package/Service 没有重复 Provider 或重复 HookSpec。

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
| F31.1 | 依赖矩阵和 Owner 基线 | 依赖图、迁移表、架构门禁初版 |
| F31.2 | Session/MessageBus 公开边界 | AgentSessionView、AgentEvent 契约 |
| F31.3 | Tools/Prompt/Profile 公开边界 | ToolRuntime、PromptAssembler、ProfileResolver |
| F31.4 | LLM/Hook 边界接入 F30 | Agent Runtime 调用契约 |
| F31.5 | 具体依赖扫描与测试替身 | Fake Service、AST 门禁、回归测试 |
| F31.6 | 文档验收 | 迁移矩阵、执行报告、TODO 更新 |

F31 完成后才进入 F32。F32 才允许修改 AgentLoop/TurnExecutor 的构造和调用实现。

---

## 11. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-25 | 初始草案：先收敛 Agent Runtime 依赖的 Service 边界，再进行 Agent Runtime Package 化 | Agent Runtime 当前依赖多个 Service 的具体 Provider/Repository/Manager，直接移动会把架构债务带入新 Package |
