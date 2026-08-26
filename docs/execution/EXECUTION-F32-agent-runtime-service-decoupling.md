# F32 Agent Runtime Service 化与具体实现解耦执行报告

## 1. 执行结论

- 仓库：`E:\\ftre`
- 分支：`feature/F32-agent-runtime-service-decoupling`
- 范围：F32.1–F32.7；只改 Host 的 Agent Runtime 接线、公开 Service API、架构门禁、回归测试和文档。
- 未修改：`E:\\ftre-agent-core`、桌面客户端、Inbox/Queue wire、Session JSON wire、Cordis Kernel。
- 结论：AgentLoop/TurnExecutor 已改为消费公开 Service；旧的具体 Manager、Projection、EventBus、MCP 和 ToolRegistry 直连已从 Runtime 删除。

## 2. PRD 修订

`docs/prd/PRD-F32-agent-runtime-service-decoupling.md` 在开发前按真实代码定稿，开发期间状态为“开发中”，验收时改为“已验收”。本次修订：

1. 允许文件清单补充 Runtime 私有 `factory.py`、Workspace accessor、Profile Manager 清理、MCP View preparer 和受影响测试；未扩大到 Core、客户端或 Queue。
2. 将不存在的 `build_view()` 改为实际公开 `ToolService.prepare_view()`，并明确 MCP 通过可逆 view preparer 接入 Tools Owner。
3. 将 Profile team-member 选择写成现有 `resolve_for_inbound()`，返回原有 `EffectiveProfile`，不新增 DTO。
4. 将 LLM 伪代码改为实际的 `ftre_llm.LlmServiceAdapter`；Core 仍负责 ReAct 流，ftre Runtime 不创建 Handler、不复制 Chunk 协议。
5. 保留既有 `BusMessage` 作为 MessageBus 的传输信封，但统一经 `MessageBusService.publish_outbound()` 发送；没有新增 Channel 协议。
6. 确认恢复继续使用 Core `UserConfirmResultEvent`，不增加 Host `Confirm` Protocol 或 Port。

## 3. 真实依赖图变化

### 3.1 迁移前的债务

| 位置 | 直接依赖 | 问题 |
|---|---|---|
| `runtime/provider.py` | `ctx.message_bus.bus`、`ctx.channels.manager`、`ctx.tools.registry`、`ctx.get("mcp")`、`ctx.agent_profiles.manager` | Provider 把多个具体实现拼进 Agent Runtime |
| `runtime/engine.py` | `EventBus`、`ToolRegistry`、`session_manager.projection`、BusMessage 转换 | Runtime 兼任总线、投影和 Agent 输入适配 |
| `runtime/turn_executor.py` | `tool_registry_for_agent()`、`AgentManager.create_agent()`、`_default_agent_state()`、`WorkspaceAccessor`、全局 `load_config()`、Projection 收尾 | Turn 直接持有其他 Owner 的内部实现 |

### 3.2 迁移后唯一入口

```text
Agent Plugin
└─ build_runtime(ctx)
   └─ AgentLoop
      └─ TurnExecutor
         ├─ sessions.finish_open_replies()/get_context_messages()/消息 CRUD
         ├─ message_bus.publish_outbound()
         ├─ session_events.emit()（先投影再广播）
         ├─ tools.prepare_view()（含 MCP preparer 和权限过滤）
         ├─ profiles.resolve_for_inbound()
         ├─ system_prompt.assemble_result()
         ├─ ConfigService.resolve_agent_config()
         ├─ WorkspaceService.create_accessor()
         ├─ ftre_llm.LlmServiceAdapter → Core ReActAgent
         └─ HookRuntime.dispatch()
```

Runtime 现在只保存显式注入的 `message_bus`、`sessions`、`tools`、`workspaces`、`profiles`、`config_service`、`system_prompt`、`llm_service`、`hooks` 和 `session_events`；没有 `ChannelManager`、MCP Service、全局 ToolRegistry、Profile Manager 或 Session Projection 字段。

## 4. F32.1–F32.6 实现

### F32.1 Provider/Runtime 构造图

- `src/ftre/services/agent/plugin.py` 的 inject 删除 `channels`，补充 `config`、`workspaces`。
- `runtime/provider.py` 只将公开 Service 传给 `AgentLoop`；attachments/traces 仅由 Provider 解析为可选能力。
- AgentLoop 的入口与 `TurnExecutor` 均以 `InboundMessage` 作为数据面输入，不再在 Runtime 内把 BusMessage 传播成 Turn 输入。

### F32.2 Session 上下文与 open reply

- `SessionService.finish_open_replies()` 成为 open reply 终态写入的公开窄方法。
- TurnExecutor 的异常、取消和收尾路径只调用该方法；Runtime 不访问 `.projection`、Repository 或 Session 目录。
- `SessionEventService` 接收 `MessageBusService`，继续保持“投影成功后再广播”的顺序。

### F32.3 MessageBus/SessionEvent 出口

- `MessageBusService.publish_outbound(BusMessage)` 统一承接 Agent 状态与命令/Channel 结果的出站发布。
- Command 与 WebSocket Plugin 的结果发布也改用窄 Service 出口；具体 Channel 构造仍由 Channel Owner 管理。
- 未增加新的 wire 消息类型，也没有重复广播 Session Projection 事件。

### F32.4 Tools/Profile/Prompt/Workspace

- `ToolService.prepare_view()` 在每个 Turn 前创建隔离 Core `ToolRegistry`，先运行可逆 view preparer，再注册内置/Plugin 工具并应用 profile allow/deny。
- MCP Plugin 通过 `register_view_preparer()` 接入，不再由 Agent Runtime 查找 MCP。
- Profile Service 新增上下文感知的 `resolve_for_inbound()`，Team 绑定解析留在 Profile Owner；Runtime 不读取 Manager 或目录。
- Workspace accessor 从 tools builtin 私有模块移到 Workspace Owner；Runtime 只调用 `WorkspaceService.create_accessor()`。
- `ConfigService.resolve_agent_config()` 是 Runtime 读取 AgentConfig 的唯一 Service 入口。

### F32.5 Core Agent 创建边界

- 新增 `runtime/factory.py`，集中 `default_agent_state()`、system prompt 环境事实拼装和 `create_core_agent()`。
- 删除 `AgentManager.create_agent()`、`AgentManager._default_agent_state()`、`AgentManager._compose_system_prompt()` 及其重复 Bash 规则常量。
- Core 仍由 `ftre-agent-core` 提供，F32 未修改 Core 仓库。

### F32.6 LLM 与 Hook

- TurnExecutor 只创建 `LlmServiceAdapter` 并传给 Core；Retry/Fallback/Compaction 既有 Hook 和 Core 语义保持不变。
- Runtime 没有 `create_llm_handler()`、Provider 原始响应处理或第二套 Chunk 协议。
- 确认恢复的 `Turn.confirm_event` 标注为 Core `UserConfirmResultEvent`。

## 5. 删除项与工程卫生

- 删除 `src/ftre/services/tools/builtin/_workspace.py`，消除 Tools Owner 对 Workspace 私有实现的反向依赖。
- 删除 AgentManager 中 201 行重复 Core Agent 构造/权限/Prompt 代码，迁移到 Runtime 唯一私有工厂。
- 清理受影响测试中的旧 `session_manager`、`loop.bus`、`build_view()`、Manager 工厂调用。
- 新增 `WorkspaceAccessor` 不是新协议，而是既有同步工具适配器迁入 Workspace Owner。
- 未跟踪的敏感临时脚本 `verify_model.py`、`cross_verify.py` 已删除；其中出现的两个 API Key
  已暴露在本地历史/终端上下文，必须由密钥所属服务立即吊销并重新生成（本任务不访问外部账户）。
- `__pycache__`、`.pytest_cache` 和 `.ruff_cache` 均为忽略的测试生成物，不进入提交；Git 工作树不包含它们。
- 执行前已存在的 F33/F34 文档规划保存在 `stash@{0}`，未混入 F32 提交。

## 6. 测试与验收

| 命令 | 结果 |
|---|---|
| `python -m pytest -q tests/architecture tests/contracts tests/startup` | `186 passed in 49.76s` |
| `python -m pytest -q tests/lifecycle ...` | `262 passed`（专项回归） |
| `python -m pytest -q` | `630 passed in 278.34s` |
| `python -m ruff check src tests packages` | `All checks passed` |
| `git diff --check` | 通过 |
| Gateway/HTTP/WebSocket smoke | `GET /api/health` → `200 {"status":"ok"}`；Composition 正常 close；F12 WebSocket smoke 在启动专项中通过 |

LLM Hook 捕获修复后追加的验证：架构专项已包含回调静态门禁；Gateway smoke 无 `adapters-updated` 异步异常。

专项门禁覆盖：Provider/Manifest 唯一 Owner、Runtime 无 `ctx.get()`/EventBus/具体 Manager、InboundMessage 单一输入、Service 公共出口、MCP view preparer 可逆、Confirmation resume、取消/删除收尾、Workspace 非 UTF-8 `.gitignore` 保留和现有 F12 WebSocket 行为。

## 7. F34 精确输入

F32 完成后，F34 可以直接以以下事实为前置：

1. Agent Runtime 的唯一构造入口是 `runtime/provider.py → build_runtime()`；F34 不再处理旧 Manager/Projection/EventBus 直连债务。
2. AgentService 的公开边界仍是 `InboundMessage → AgentDriver.run()`；AgentLoop/TurnExecutor 仍留在 Host，尚未抽 Package。
3. Tools、Profile、Workspace、Session、MessageBus、Prompt、LLM 和 Hook 的注入 key/调用入口已冻结，可作为 Agent Package 的稳定依赖矩阵。
4. Core Agent 的唯一 Host 构造函数是 `runtime/factory.create_core_agent()`；F34 若抽包，只需迁移这一处和明确的 Service 契约，不得恢复 Manager/Facade/Port。
5. Inbox/Queue、Session wire、客户端和 Core 均保持原协议，F34 不应把 Agent Package 抽取与协议重写混在同一批。

## 8. Git 交付

| 提交 | 内容 |
|---|---|
| `e249b51` | F32 PRD 修订、TODO 启动并标记开发中 |
| `01cafa9` | Agent Runtime 与公开 Service 的生产接线迁移、重复 Owner 删除 |
| `075d672` | F32 架构门禁、契约测试和生命周期回归 |
| `0d79d5f` | 修复 LLM adapters-updated 回调的 Context Locator 访问，并补充架构回归门禁 |
| `456b306` | 最终验收报告、CHANGELOG、PRD/TODO 收尾 |

未 push、未 merge；提交前后均按 `docs/COMMIT.md` 校验提交格式。最终交付要求工作树除执行前明确保留的外部仓库修改外保持干净。
