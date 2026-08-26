# F31 Agent Runtime Service 边界与契约基线执行报告

## 1. 执行结论

- 仓库：`E:\\ftre`
- 分支：`feature/F31-agent-service-boundaries`
- 范围：F31.1–F31.6；只建立事实基线、契约测试和架构门禁。
- 明确未修改：`E:\\ftre-agent-core`、客户端、`ftre-inbox`/Queue、Session wire、
  `AgentLoop`/`TurnExecutor` 生产实现。
- 结论：F31 已按收窄后的 PRD 完成；运行时接线迁移由 F32 承接。

## 2. PRD 修订

`docs/prd/PRD-F31-agent-service-boundaries.md` 已从草案重写为事实基线，状态为“已验收”。
本次修订完成：

1. 删除虚构的 `AgentSessionView`、`AgentEventSink`、`AgentToolRuntime`、
   `PromptAssembler`、`AgentProfileResolver`、`EffectiveAgentProfile` 和 `AgentEvent` 契约；
2. 修正 `agent/request` 的发布者为 `ftre_llm.service.LlmService`，
   `llm/error`/`agent/stop-decision` 的发布者为 Agent Core；
3. 删除 `ftre-agent` Package 前置依赖及与 F32 重叠的 Runtime 构造迁移承诺；
4. 补充真实 `AgentService` 公开边界，并将既有 `AgentDriver` 标为组合/测试契约，不新增同义 Port；
5. 以当前源码签名、属性访问和调用点替换抽象猜测，新增 F32 精确输入和删除条件；
6. 变更记录说明收窄原因，并将 AC1–AC12 逐条复核为通过。

## 3. F31.1–F31.4 事实证据

### 3.1 Runtime 依赖矩阵

矩阵文件：`docs/execution/matrices/F31-agent-service-boundaries.md`。

| Owner/债务 | 代码证据 | F32 输入 |
|---|---|---|
| Agent Provider | `src/ftre/services/agent/plugin.py` 的 `inject/provide/apply` | 维持一个 `agents` Owner，不创建第二 Runtime Service |
| Bus 具体实例 | `runtime/provider.py:29` 的 `ctx.message_bus.bus`；`engine.py:586` 出站发送 | 给 `MessageBusService` 增加窄 `publish_outbound` |
| Channel Manager | `runtime/provider.py:33`；`AgentLoop.__init__` 参数 | 从 Runtime 构造图删除 |
| Tool Registry/MCP | `provider.py:37,40`；`engine.py:67,69`；`turn_executor.py:414` | 由 Tools Owner 创建 View，MCP 不进入 Runtime |
| Profile Manager | `provider.py:42`；`turn_executor.py:347,377,428,816` | 只消费 `agent_profiles.resolve()` 快照 |
| Session Projection | `engine.py:130`；`turn_executor.py:624` | 收口到 `sessions.finish_open_replies()` |
| Workspace/Config | `turn_executor.py:45,442,742` | 删除 Runtime 对具体 Workspace/全局配置的直接访问 |

矩阵同时记录 `InboundMessage → BusMessage` 适配、当前 Service key、Plugin Owner 和删除条件；
未提前移动任何生产目录。

### 3.2 Service/Plugin Owner 与公开 API

- `agents`：`AgentService.run/cancel/status/is_busy/delete_session/resume_confirmation`，
  以及 registry scope 查询；Loop/TurnExecutor/Driver 对象不作为业务 API。
- `sessions`：`get_session/get_session_metadata/get_context_messages/save_message/update_message/
  upsert_message`；Projection 是内部实现，`finish_open_replies` 是 F32 缺口。
- `message_bus`：当前 inbound 方法为 `publish_inbound/request_inbound/stop_inbound`；底层
  EventBus 出站能力已登记，窄 Service 出口由 F32 补齐。
- `tools`：`schemas/build_view/execute`；MCP、Workspace、Attachment 保持独立 Owner。
- `system_prompt`：同步 `assemble_result(...) -> PromptAssembly`，没有异步 Prompt Protocol。
- `agent_profiles`：同步 `resolve(...) -> EffectiveProfile`，`value: Any` 过宽问题登记为 F32 债务。
- `llm`：沿用 F30 `prepare_call/PreparedLlmCall.stream/stream`，不直接创建 Core Handler。
- `hook_runtime/session_events`：分别使用现有 `dispatch(...)`、`emit(...)`，没有第二 Dispatcher
  或第二出站 Owner。

### 3.3 Hook Owner

`tests/architecture/test_f31_agent_service_boundaries.py` 锁定 8 个唯一 Spec：
`agent/before-run`、`agent/after-run`、`agent/run-error`、`agent/request`、`llm/stream`、
`llm/error`、`agent/stop-decision`、`system-prompt/assemble`，并验证发布域、模式和类型来源。
`packages/ftre-llm/src/ftre_llm/service.py` 的真实源码证明 `agent/request` 由 LlmService 发布，
Runtime 没有复制同名发布逻辑。

## 4. F31.5 门禁与契约测试

新增：

- `tests/architecture/test_f31_agent_service_boundaries.py`：Provider/Manifest 唯一 Owner、
  Runtime 存量具体依赖不增、`ctx.get()` Service Locator 禁止、跨 Owner 私有 import 基线、
  Hook Spec 唯一性和 LLM 发布者扫描；
- `tests/contracts/test_f31_service_contracts.py`：AgentService InboundMessage 边界、Session/
  MessageBus 方法集合、Tool View、同步 PromptAssembly、EffectiveProfile 和测试 Fake。

Fake 只存在于测试文件，不声明生产 Protocol/Port；测试不使用真实 API Key，也没有 `skip`/`xfail`。

## 5. 验证记录

| 命令 | 结果 |
|---|---|
| `python -m pytest -q tests/architecture tests/contracts tests/startup` | `183 passed in 34.30s` |
| `python -m pytest -q` | `626 passed in 185.35s` |
| `python -m ruff check src tests packages` | `All checks passed` |
| `git diff --check` | 通过 |

专项测试和全量回归均未触及 Core、客户端、Inbox/Queue 或 Session wire。

## 6. F32 精确输入

F32 开工时按矩阵第 6 节执行：

1. `runtime/provider.py` 改为注入 `message_bus`，不再读取 `.bus`；
2. 删除 AgentLoop 的 `channel_manager/mcp_service/tool_registry/agent_manager/session_projection`；
3. Turn 使用 `InboundMessage` 快照，不在 Runtime 内继续扩散 BusMessage；
4. SessionService 增加 `finish_open_replies()`，保持现有 Projection 的落库并发语义；
5. Profile/Tools 隐藏 `.manager`/`.registry`，Core 必需 ToolRegistry 仍由 Tools Owner 创建；
6. 删除 Runtime 对 WorkspaceAccessor、全局 `load_config()` 和 Manager 私有方法的直接访问；
7. 每项删除配套回归测试，更新本报告对应 AST 基线。

F32 不应扩大为 Agent Package 抽取或客户端/队列协议改造。

## 7. Git 交付

| 提交 | 内容 |
|---|---|
| `71b1f60` | 定稿 F31 PRD、Owner/依赖矩阵，启动 F31 TODO |
| `c3c643c` | Fake Service 契约测试、Owner/Hook/AST 架构门禁 |
| `576ce96` | F31 验收报告、CHANGELOG、PRD/TODO 收尾 |
| `13fd793` | 收紧全部 Agent Runtime 文件的跨 Owner 私有导入扫描 |

提交前后均执行 `Get-Content docs/COMMIT.md` 复核规范；未 push、未 merge、未修改 develop。

## 8. 工程卫生

F31 新增源码和测试产生的缓存均未被 Git 跟踪；仓库中已存在的 F33/F34 草案及其 TODO 规划属于
本阶段之前的未提交工作，未纳入 F31 提交。最终收尾时只保留这些原有改动（或以可恢复 stash
保存），不删除用户数据、不执行宽泛递归清理。
