# F31 Agent Runtime Service 边界迁移矩阵

## 1. 范围

本矩阵是 F31.1–F31.6 的事实基线，只描述 `E:\\ftre` 当前 `develop` 代码。它不移动
AgentLoop/TurnExecutor，不修改 Agent Core、客户端、Inbox/Queue 或 Session wire；F32 以本表
作为运行时接线迁移的输入。

术语约定：

- **Service**：通过 Cordis Context 提供、拥有稳定运行时能力的对象；
- **Plugin**：创建、注册、启停和清理 Service/行为的生命周期 Owner；
- **Hook**：由发布者定义时机、由 Plugin 监听的扩展契约；
- **Runtime 债务**：当前仍存在、但已确定由 F32 删除或收敛的具体实现访问。

## 2. 当前真实调用链

```text
Channel / Command
        ↓
MessageBusService / ftre-inbox
        ↓  InboundMessage
AgentService.run()
        ↓ AgentLoop.run_inbound()
        ├─ AgentLoop 生成 BusMessage（当前适配债务）
        ├─ SessionService 写 UserMessage
        └─ TurnExecutor.execute(BusMessage)
             ├─ SessionService.get_context_messages()
             ├─ SystemPromptService.assemble_result()
             ├─ AgentProfileService 背后的 AgentManager
             ├─ ToolService.build_view() → Core ToolRegistry
             ├─ LlmServiceAdapter → Core ReActAgent
             └─ SessionProjection / MessageBus 出站（当前具体实现访问）
```

## 3. Runtime 依赖基线

| 文件/符号 | 当前依赖或访问 | 当前 Owner | F32 目标 | 删除条件 |
|---|---|---|---|---|
| `runtime/provider.py::build_runtime` | `ctx.message_bus.bus` | `MessageBusService` | 传入 `ctx.message_bus` | MessageBusService 提供出站窄方法 |
| `runtime/provider.py::build_runtime` | `ctx.channels.manager` | Channel Plugin | 不进入 Agent Runtime | Runtime 只发布出站 BusMessage |
| `runtime/provider.py::build_runtime` | `ctx.tools.registry` | `ToolService` | `tools.build_view()` | Core View 创建完全由 Tools Owner 负责 |
| `runtime/provider.py::build_runtime` | `ctx.agent_profiles.manager` | `AgentProfileService` | `agent_profiles.resolve()` | Profile Service 提供可消费快照 |
| `runtime/provider.py::build_runtime` | `ctx.get("mcp", strict=False)` | MCP Plugin | 不进入 Runtime | Tools/Plugin 自己准备 MCP |
| `runtime/engine.py::AgentLoop.__init__` | `channel_manager`, `tool_registry`, `mcp_service`, `agent_manager` | 多个 Provider | 删除参数/字段 | F32 构造图迁移完成 |
| `runtime/engine.py::AgentLoop.__init__` | `session_manager.projection` | SessionService | 只保留 `sessions` | SessionService 提供公开收尾方法 |
| `runtime/engine.py::run_inbound` | `InboundMessage → BusMessage` | Agent Runtime | Turn 使用 InboundMessage 快照 | F32 输入边界迁移 |
| `runtime/turn_executor.py::_create_agent` | `loop.agent_manager.create_agent()` | AgentProfileService 背后的 Manager | Runtime 私有 `create_core_agent()` | Core 创建参数由 Runtime 明确组装 |
| `runtime/turn_executor.py::_build_resume` | `loop.agent_manager._default_agent_state()` | AgentProfile/权限构造 | Runtime 内部明确状态工厂 | 不访问 Manager 私有方法 |
| `runtime/turn_executor.py::_persist_open_replies` | `loop.session_projection.finish_open()` | SessionService Projection | `sessions.finish_open_replies()` | SessionService 公开窄方法 |
| `runtime/turn_executor.py::_create_agent` | `WorkspaceAccessor(...)` | Workspace/Tool Context | Tool Service/Runtime Context | Runtime 不构造 WorkspaceAccessor |
| `runtime/turn_executor.py::_resolve_turn_config` | `load_config()`、`agent_manager.load()`、sub-agent 私有 helper | Config/Profile Owner | Profile/Config 快照 | Runtime 只消费解析结果 |

上述条目是 F31 允许存在的完整基线；新增任何同类依赖必须使 F31 架构测试失败。

## 4. Service Owner 与真实公开 API

| Service key | Provider Plugin | 当前真实入口 | F31 冻结结论 |
|---|---|---|---|
| `sessions` | `services/session/plugin.py` | `get_session`、`get_session_metadata`、`get_context_messages`、`save_message`、`update_message`、`upsert_message` | 直接扩展现有 SessionService；不创建 `AgentSessionView` |
| `message_bus` | `services/messaging/bus/plugin.py` | `publish_inbound`、`request_inbound`、`stop_inbound`；底层 EventBus 有 `publish_outbound` | F32 补 `MessageBusService.publish_outbound(BusMessage)`，不创建 `AgentEvent` |
| `tools` | `services/tools/plugin.py` | `schemas`、`build_view`、`execute` | Core 必需 `ToolRegistry` 作为登记的兼容对象，不由 Runtime 创建 |
| `system_prompt` | `services/system_prompt/plugin.py` | 同步 `assemble_result(...)`、`assemble(...)` | 不创建异步 Prompt Protocol |
| `agent_profiles` | `services/agent/profile/plugin.py` | `resolve(agent_id, session_id=None)` → `EffectiveProfile` | 保留现有类型并登记 `value: Any` 过宽债务，不创建第二 Profile DTO |
| `llm` | `services/llm/plugin.py` + `ftre-llm` | `prepare_call`、`stream` | 遵循 F30；Runtime 不调用 `create_llm_handler` |
| `hook_runtime` | Composition `context.provide` | `dispatch(spec, payload, context=...)` | 不创建第二 Dispatcher |
| `session_events` | `services/session/plugin.py` | `emit(session_id, channel_id, event, metadata=...)` | 继续负责 Session 事实先落库后广播 |

MCP、Workspace、Attachment、Channel 各自拥有独立 Service/Plugin；本矩阵不把它们并入 Tools。

## 5. Hook Owner 矩阵

| Hook | 真实定义/发布者 | 监听者 | 模式/结果 | F31 结论 |
|---|---|---|---|---|
| `agent/before-run` | `services/agent/hooks.py` / AgentLoop | 治理、权限 Plugin | Waterfall：`AllowRun`/`RejectRun` | Host Agent Runtime Hook |
| `agent/after-run` | `services/agent/hooks.py` / AgentLoop | Compaction/维护 Plugin | Waterfall：`None` | Host 收尾屏障，不移动压缩 Owner |
| `agent/run-error` | `services/agent/hooks.py` / TurnExecutor | Host 恢复行为 | Waterfall：`RetryRequest`/`None` | Agent Run 错误，不等同 Core LLM 错误 |
| `agent/request` | `ftre_llm.service.LlmService` | 模型选择/Fallback Plugin | Waterfall：`LlmCallConfig` | 不是 AgentLoop 发布 |
| `llm/stream` | `ftre-llm` 与 Core 共用 Spec | 计量/Fallback/包装 Plugin | Waterfall：流 | 不重复声明 |
| `llm/error` | Agent Core | `ftre-llm-recovery` | Waterfall：`LLMErrorDecision`/`None` | Retry Owner 在 Core Hook 链 |
| `agent/stop-decision` | Agent Core | Steering/Continuation Plugin | Waterfall：`StopTurn`/`ContinueTurn` | 仅 Core 发布 |
| `system-prompt/assemble` | `SystemPromptService`/Host | Prompt Feature | Waterfall：`PromptAssembly` | 复用现有契约 |

## 6. F32 精确输入

F32 开工时必须按下列顺序处理，不得扩大为 Package 抽取：

1. Provider 从 `ctx.message_bus` 注入公开 Service，不再读取 `.bus`；
2. AgentLoop 删除 `channel_manager`、`mcp_service`、全局 `tool_registry`、`agent_manager`、
   `session_projection` 字段；
3. TurnExecutor 只接收 `InboundMessage`，并通过 `sessions`、`tools`、`system_prompt`、
   `agent_profiles`、`llm` 和 `session_events` 完成调用；
4. Session 收尾增加 `finish_open_replies()`，保持 Projection 的持久化和并发语义；
5. Profile Service 隐藏 `.manager`，Tools Service 隐藏 `.registry`；
6. Core 仍需要的 `ToolRegistry` 只能由 Tools Service 创建并返回，不能在 Runtime 复制第二个；
7. 每个删除项都必须有回归测试和旧引用 AST 门禁。

F31 不修改以上运行时实现；F31 只冻结名称、Owner、删除顺序和验证方法。

## 7. 基线扫描规则

- Runtime 文件不得新增 `ctx.get()`、`SessionRepository`、`SessionProjection`、Channel/MCP
  concrete import 或第二个 LLM 工厂；
- 已登记的 F31 债务可以存在，但数量必须与第 3 节一致；
- Provider 的 optional `ctx.get("attachments"/"traces"/"mcp")` 必须位于 Provider 构造边界，
  并且 key 已声明或明确标记 optional；
- 测试 Fake 只用于验证方法调用形状，不得成为生产 Protocol、Port 或 Service Locator；
- 任何 Service/Plugin/Hook 新增 Owner、同名 key 或重复 Spec 都必须失败。
