# PRD-F32 Agent Runtime Service 化与具体实现解耦

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F32 |
| 名称 | Agent Runtime Service 化与具体实现解耦 |
| 状态 | 开发中 |
| 创建日期 | 2026-08-25 |
| 定稿日期 | 2026-08-26 |
| 验收日期 | 待开发 |
| 关联文档 | `docs/TODO.yaml` F32；`docs/prd/PRD-F30-llm-service-package.md`；`docs/prd/PRD-F31-agent-service-boundaries.md`；`AGENTS.md` |

---

## 1. 背景与目标

### 1.1 当前问题

F31 已经确认 ftre 拥有多个 Service，但 Agent Runtime 仍通过具体实现和内部属性使用它们。
当前构造图位于：

- `src/ftre/services/agent/runtime/provider.py`
- `src/ftre/services/agent/runtime/engine.py`
- `src/ftre/services/agent/runtime/turn_executor.py`

当前 Provider 直接组装：

```python
{
    "bus": ctx.message_bus.bus,
    "session_manager": ctx.sessions,
    "channel_manager": ctx.channels.manager,
    "tool_registry": ctx.tools.registry,
    "tool_service": ctx.tools,
    "mcp_service": ctx.get("mcp", strict=False),
    "agent_manager": ctx.agent_profiles.manager,
    "attachments": ctx.get("attachments", strict=False),
    "traces": ctx.get("traces", strict=False),
    "system_prompt": ctx.system_prompt,
    "hook_runtime": ctx.hook_runtime,
    "session_events": ctx.session_events,
}
```

TurnExecutor 还直接访问：

```python
loop.agent_manager.create_agent(...)
loop.agent_manager._default_agent_state()
loop.session_projection.finish_open(...)
loop.tool_registry_for_agent(...)
WorkspaceAccessor(...)
```

这些调用使 Agent Runtime 仍然绑定 Session Repository/Projection、Agent Profile Manager、
MCP、Workspace、ToolRegistry 和 Channel 具体实现，无法在不携带架构债务的情况下进入下一阶段
Package 化。

### 1.2 目标

本阶段在现有 `src/ftre/services/agent/runtime/` 位置完成一次真实的 Service 接线迁移：

```text
InboundMessage
    ↓
AgentLoop
    ↓ Inject 公开 Service
TurnExecutor
    ↓
Agent Core + Service 合约
```

完成后，Agent Runtime 仍留在 Host，不移动 Package，但不再直接依赖 ChannelManager、MCP、
AgentManager、Session Projection、WorkspaceAccessor 或 LLM Handler 工厂。

### 1.3 非目标

- 不创建 `packages/ftre-agent` 或 `packages/ftre-agent-runtime`；
- 不移动 `AgentLoop`、`TurnExecutor`、`Driver`、`Provider` 文件；
- 不改变客户端 WebSocket/HTTP 协议、Queue wire、Inbox claim 或 Steer 语义；
- 不修改 Agent Core 仓库或 Core 的公共 API；若发现 Core API 阻塞，必须登记跨仓 blocker，
  不在本阶段偷偷修改；
- 不实现新的 Retry、Fallback 或 Compaction 算法；
- 不把 ChannelManager 改成 Agent Service 依赖；
- 不为每个具体类新增 Port、Facade、Coordinator 或 Service Locator。

---

## 2. 前置条件与完成边界

### 2.1 前置条件

- F30 的 `llm` Service 已完成并可由 Agent Runtime 注入；
- F31 的 Service Owner、目标公开契约和依赖矩阵已经评审通过；
- `sessions`、`message_bus`、`tools`、`system_prompt`、`agent_profiles`、`hook_runtime`、
  `session_events` 已提供本阶段所需的公开方法，或在本阶段明确补齐；
- 现有 Agent/Core、Session、Inbox 和客户端回归基线可运行。

### 2.2 F32 允许修改的代码

```text
src/ftre/services/agent/runtime/{provider.py,engine.py,turn_executor.py}
src/ftre/services/agent/runtime/factory.py
src/ftre/services/agent/profile/{service.py,plugin.py,manager.py}
src/ftre/services/messaging/bus/service.py
src/ftre/services/session/{service.py,events.py,plugin.py}
src/ftre/services/tools/{service.py,plugin.py,builtin/*.py}
src/ftre/services/system_prompt/service.py
src/ftre/services/config/service.py
src/ftre/services/workspace/{service.py,accessor.py}
src/ftre/plugins/builtin/{command/plugin.py,channels/websocket/plugin.py,mcp/plugin.py}
tests/**/*.py（仅更新受影响的契约、生命周期和回归测试）
```

### 2.3 不允许修改的代码

```text
E:\ftre-agent-core\
客户端仓库
Inbox Package 的 Queue/claim 协议
Session JSON/SQLite wire 格式
Cordis Kernel 机制
```

---

## 3. 目标 Inject 图

Agent Runtime 的 Provider 只解析公开 Service：

```python
inject = (
    "llm",
    "config",
    "sessions",
    "tools",
    "workspaces",
    "system_prompt",
    "agent_profiles",
    "message_bus",
    "hook_runtime",
    "session_events",
)
```

可选能力由 Provider 显式传入 `None`，Runtime 不自行 `ctx.get()`：

```python
attachments = ctx.get("attachments", strict=False)
traces = ctx.get("traces", strict=False)
```

目标构造：

```python
runtime = AgentLoop(
    message_bus=ctx.message_bus,
    sessions=ctx.sessions,
    tools=ctx.tools,
    workspaces=ctx.workspaces,
    profiles=ctx.agent_profiles,
    config_service=ctx.config,
    system_prompt=ctx.system_prompt,
    llm_service=ctx.llm,
    hooks=ctx.hook_runtime,
    session_events=ctx.session_events,
    attachments=attachments,
    traces=traces,
    agent_service=agent_service,
)
```

必须删除的 Runtime 构造参数：

```text
channel_manager
tool_registry
mcp_service
agent_manager
session_projection
```

`AgentService` 仍由 Provider 创建并绑定 Driver；F32 不移动它，也不创建第二个 Service Owner。

---

## 4. Service 接线改造

### 4.1 Session Service

当前 Runtime 使用：

```python
get_session()
get_session_metadata()
get_context_messages()
save_message()
update_message()
session_projection.finish_open()
```

F32 要求：

1. 保留已有 `SessionService` 作为唯一公开入口；
2. Runtime 通过 Service 方法读取 Session 和上下文；
3. 将 `session_projection.finish_open()` 收敛为 `sessions.finish_open_replies()` 窄方法；
4. 不让 Runtime 访问 `.projection`、`.repository` 或持久化目录；
5. 不新增通用 `SessionPort`；现有 SessionService 直接补充公开方法。

目标调用：

```python
for message in await self._sessions.finish_open_replies(
    turn.session_id,
    reason,
    error=error,
):
    turn.final_content = message.get_text_content() or turn.final_content
```

`finish_open_replies()` 的持久化行为必须与当前 Projection 实现一致，不改变 Session wire。

### 4.2 MessageBus 与 Session Events

当前 `MessageBusService` 主要提供 inbound API，而 AgentLoop 直接调用底层
`EventBus.publish_outbound()`。F32 要明确两个 Owner：

```text
message_bus
└─ 进程内传输和 BusMessage 出站发布

session_events
└─ Session 事件先持久化、再广播权威事实

channel plugin
└─ 消费出站事件并转换客户端协议
```

F32 允许为 `MessageBusService` 补充最小出口：

```python
async def publish_outbound(self, message: BusMessage) -> None: ...
```

但 Session Projection 相关事件仍必须走 `session_events.emit()`，不能重复通过两个出口广播。

目标调用：

```python
await self._message_bus.publish_outbound(
    BusMessage(
        type="session/status",
        from_channel=channel_id,
        to_channel=channel_id,
        from_session=session_id,
        to_session=session_id,
        data={"session_id": session_id, "status": "running"},
    )
)
```

Agent Runtime 不再持有 `ChannelManager`；出站仍使用既有 `BusMessage`，但只能通过
`MessageBusService.publish_outbound()` 发送，不能读取 `.bus` 或直接调用底层 EventBus。

### 4.3 Tool Service

当前 ToolService 已提供 `schemas()`、`execute()`，但 Agent Runtime 还直接
拿 `ToolRegistry`，并在 AgentLoop 中调用 MCP 准备逻辑。

F32 要求：

1. `ToolService` 提供按 Agent/Profile 生成 Tool View 的公开方法；
2. MCP 准备在 ToolService Provider 内完成；
3. Runtime 不直接 `ctx.get("mcp")`；
4. Runtime 不持有全局 ToolRegistry；
5. Core 仍需要的 Tool View 只作为 Service 返回的 Core 兼容对象传入，不在 Runtime 内创建第二
   个 Registry；
6. Tool 执行、取消和结果归一化保持现有 Core Tool Hook 语义。

目标接口：

```python
async def prepare_view(
    self,
    agent_id: str,
    session_id: str,
    profile_config: Mapping[str, Any] | AgentProfile | None = None,
) -> ToolView:
    ...
```

Runtime 只调用：

```python
tool_view = await self._tools.prepare_view(
    agent_id,
    turn.session_id,
    profile_config,
)
```

`ToolView` 是 Core 当前要求的 `ToolRegistry` 实例；其创建、MCP 准备和权限过滤 Owner
必须是 ToolService，Runtime 只传递返回值，不保存全局 Registry。

### 4.4 System Prompt Service

当前 `SystemPromptService.assemble_result()` 已经是同步公开方法，F32 不再新建异步
`PromptAssembler` Service，也不创建第二个 Prompt DTO。

目标是直接使用现有公开边界：

```python
assembly = self._system_prompt.assemble_result(
    agent_id,
    session_id,
    workspace=workspace,
    messages=messages,
    base_prompt=config.system_prompt,
)
```

F32 只要求：

- Runtime 不遍历 `_sections`；
- Runtime 不创建 `PromptSection`；
- `PromptAssembly` 继续由 SystemPromptService 生成；
- 后续 System Prompt Hook 仍由 HookRuntime 统一分发。

### 4.5 Agent Profile Service

当前 `AgentProfileService.resolve()` 返回 `EffectiveProfile`，内部仍持有 `AgentManager`。
F32 不移动 Profile 存储，但 Runtime 必须停止使用：

```python
ctx.agent_profiles.manager
agent_manager.load()
agent_manager._default_agent_state()
```

目标：

```python
profile = self._profiles.resolve(
    agent_id,
    session_id=turn.session_id,
)
```

请求还要结合 Session 的 team-member 绑定时，Runtime 调用同一 Service 的
`resolve_for_inbound(agent_id, session_id, metadata)`；该方法返回相同的 `EffectiveProfile`
快照类型，差异只在 Profile Service 内部完成，不把 Manager 或目录路径泄漏给 Runtime。

Profile Service 负责将 Profile 文件解析为本轮可消费的有效快照；Runtime 只能读取快照，不能
从中反查文件路径或 Manager。

Core `ReActAgent` 的创建移到 Runtime 内部的私有 `create_core_agent()` 函数。该函数只接收：

```python
config
profile_snapshot
tool_view
system_prompt
tracer
hooks
state
```

它不是新的公共 Service，也不成为第二个 Composition Owner。

### 4.6 LLM Service

F32 依赖 F30 的公开 `ctx.llm`：

```python
service_adapter = LlmServiceAdapter(
    ctx.llm,
    call_config,
    credentials,
    session_id=session_id,
    turn_id=turn_id,
    cancellation=turn.cancellation,
)
```

Runtime 只在私有 `create_core_agent()` 工厂中把这个 Adapter 传给 Core；具体的
`prepare_call()/stream()` 由 `ftre-llm` Adapter 交给 LlmService 完成，Runtime 不自行
创建 Handler、拼装第二套 Chunk 或消费 Provider 原始响应。

F32 不允许 Agent Runtime、Agent Profile、Compaction 或 Session Title 直接调用
`create_llm_handler()`。

---

## 5. AgentLoop/TurnExecutor 迁移

### 5.1 AgentLoop

修改：

- `session_manager` 改为 `sessions` Service；
- `bus` 改为 `message_bus` Service；
- `tool_service` 保留为唯一 Tool 入口；
- `system_prompt`、`agent_profiles`、`llm`、`hooks`、`session_events` 改为显式依赖；
- `channel_manager` 删除；
- `mcp_service` 删除；
- `tool_registry` 字段删除；
- `agent_manager` 字段删除；
- `session_projection` 字段删除；
- optional `attachments`、`traces` 由 Provider 显式传入。

### 5.2 TurnExecutor

修改：

- `InboundMessage` 在入口完成转换，`Turn.inbound` 不再保存 Host `BusMessage`；
- Session 读取和收尾只通过 `sessions` Service；
- Prompt 只调用 `system_prompt.assemble_result()`；
- Profile 只调用 `agent_profiles.resolve()`；
- Tool View 只调用 `tools.prepare_view()`；
- Core Agent 创建迁移到 Runtime 私有工厂函数；
- LLM 调用统一通过 `llm.prepare_call()/stream()`；
- 事件通过 `message_bus.publish_agent_event()` 或 `session_events.emit()`，不直接调用 Channel；
- `confirm_event` 继续使用 Core 的 `UserConfirmResultEvent`，只在 Runtime 的 `Turn` 中标注
  这个既有类型；不新增 Host DTO，也不复制 Core 事件协议。

### 5.3 Core 边界

F32 不修改 `E:\ftre-agent-core`。如果 Core 现有构造参数需要一个 Core Tool View，ftre
通过 `ToolService.prepare_view()` 提供兼容对象；如果发现必须修改 Core 的公共协议，必须：

1. 在 F32 执行报告登记 blocker；
2. 新建配对 Core PRD/阶段；
3. 不在 F32 中复制 Core 源码或添加兼容壳。

---

## 6. 生命周期和并发

- Agent Runtime 的 active Task、cancel signal、maintenance 状态仍由 Runtime 自己拥有；
- Service Provider 的 Hook、事件订阅、Tool View 和配置快照必须绑定 Plugin Fiber；
- unload 时不得留下 Agent Task、Session Event listener 或 Tool View 资源；
- `cancel()` 必须只取消指定 Run，不得影响同 Session 之后的 Inbox pending；
- Session 的 `finish_open_replies()` 必须在 Turn 收尾阶段完成，避免 Inbox 错误领取下一条；
- `message_bus` 和 `session_events` 的广播失败不能改变已经持久化的 Session 事实；
- Agent Runtime 不能保存 pending QueueItem 或自行创建第二个 worker。

---

## 7. 测试计划

### 7.1 契约测试

- Session 收尾、上下文读取、消息写入使用 Fake Session Service；
- MessageBus AgentEvent 出口和 SessionEvent 出口不重复广播；
- ToolService `prepare_view()` 按 Agent/Profile 生成隔离 View；
- Prompt 使用现有 `assemble_result()`；
- Profile resolve 返回冻结的有效快照；
- LLM 调用只走 `prepare_call()/stream()`；
- Agent Hook payload 和 `confirm_event` 明确类型。

### 7.2 架构门禁

- `AgentLoop` 不 import `ChannelManager`、`McpService`、`ToolRegistry`、`AgentManager`；
- `TurnExecutor` 不访问 `session_projection`、`.repository`、`.manager`；
- Runtime 不调用 `create_llm_handler()`；
- Runtime 不直接 `ctx.get()` 必选或可选 Service；
- Runtime 只构造既有 `BusMessage` 作为 MessageBus Service 的传输信封，不把它当作新的
  Channel 协议；所有发送必须经过 `publish_outbound()`，不得直接访问 EventBus；
- 没有新增 Port、Facade、Coordinator、Service Bag 或兼容入口；
- Core、Session、Inbox、Client 的既有协议文件未被修改。

### 7.3 回归测试

- 普通 InboundMessage；
- 多 Agent/多 Session 并发；
- Tool Call/Tool Result；
- Steering 在下一次 Reasoning 消费；
- Compaction after-run；
- Retry/Fallback；
- Confirmation resume；
- 取消、Session 删除、Gateway unload/restart、in-flight Hook。

---

## 8. 验收标准

- [ ] **AC1**：AgentLoop/TurnExecutor 的构造参数已改为公开 Service 依赖，具体依赖矩阵与实现一致。
- [ ] **AC2**：Runtime 不再持有或访问 `channel_manager`、`mcp_service`、`tool_registry`、
  `agent_manager`、`session_projection`。
- [ ] **AC3**：Session 消息读取、持久化和 open reply 收尾全部通过 `sessions` Service；Session
  Repository/Projection 私有实现没有 Runtime import。
- [ ] **AC4**：AgentEvent 通过 `message_bus` 公开出口发布，Session 事实通过 `session_events`
  发布，Channel 不再由 Agent Runtime 直接调用，且无重复广播。
- [ ] **AC5**：Tool View 和 MCP 准备由 `tools` Service Owner 完成；Runtime 不直接依赖 MCP、
  Workspace 或全局 ToolRegistry。
- [ ] **AC6**：Profile 由 `agent_profiles.resolve()` 提供有效快照；Runtime 不调用 AgentManager
  或读取配置目录。
- [ ] **AC7**：System Prompt 使用现有 `system_prompt.assemble_result()`；LLM 使用 F30 的
  `prepare_call()/stream()`；没有直接 `create_llm_handler()`。
- [ ] **AC8**：Core Agent 创建只存在一个 Runtime 私有工厂；没有新增公共 Factory Service、
  Port 或第二个 Owner。
- [ ] **AC9**：普通消息、Steer、Tool、Compaction、Retry、Fallback、Confirmation、取消和
  Session 删除回归通过。
- [ ] **AC10**：unload/restart/in-flight Hook 后没有残留 Task、Hook、Tool View 或监听器。
- [ ] **AC11**：Core、Client、Inbox wire 和 Session 持久化格式未改变；若 Core API 确实阻塞，
  已登记配对阶段而不是偷偷修改。
- [ ] **AC12**：pytest、ruff、架构扫描、契约测试、生命周期测试和 `git diff --check` 通过。

---

## 9. 实施批次

| 批次 | 内容 | 产物 |
|---|---|---|
| F32.1 | Provider/Runtime 构造图迁移 | AgentLoop 只接收公开 Service |
| F32.2 | Session 收尾和上下文访问迁移 | 移除 `session_projection` 访问 |
| F32.3 | MessageBus/SessionEvent 出口收敛 | 删除底层 EventBus/Channel 出站调用 |
| F32.4 | Tools/Profile/Prompt 迁移 | 删除 MCP、Workspace、AgentManager 直接依赖 |
| F32.5 | Core Agent 私有创建边界 | 单一 `create_core_agent()`，不新增公共 Service |
| F32.6 | LLM 统一调用和 Hook 对齐 | 删除直接 Handler 工厂调用 |
| F32.7 | 回归、生命周期和架构验收 | 执行报告、迁移矩阵、TODO 更新 |

完成 F32 后才进入 F33 `ftre-agent` Service Package 抽取；F32 不移动 Package 目录。

---

## 10. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-25 | 初始草案：在 F31 Service 边界基础上，实际改造 AgentLoop/TurnExecutor 的依赖接线 | F31 只冻结契约；F32 负责删除具体实现依赖，为 Agent Package 化做准备 |
| 2026-08-26 | 按真实调用链定稿：补充 Runtime 私有工厂、WorkspaceAccessor、Tools/MCP View preparer、Profile `resolve_for_inbound` 和 MessageBus 窄出站；明确 Core `LlmServiceAdapter` 委托、既有 `BusMessage` 信封和 Core 确认事件类型 | 删除与实现不一致的 `build_view`、虚构 LLM 直调和新 Confirm DTO 描述，避免把迁移层误写成第二套协议 |
