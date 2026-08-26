# ftre F30–F32 架构交接文档

> 用途：交给下一位 Agent 继续开发前阅读。本文记录本轮关于轻内核、Service、Plugin、
> Hook、LLM、Agent Runtime、Inbox/Queue 的关键决策、实际代码落点、验证证据和未完成事项。
>
> 安全说明：本文不记录任何 API Key、用户凭据或运行时 session 内容。

## 1. 当前仓库状态

| 项目 | 当前事实 |
|---|---|
| 仓库 | `E:\ftre` |
| 当前分支 | `develop` |
| 最新合并 | PR #61，合并提交 `07545ad` |
| F30 | 已验收 |
| F31 | 已验收 |
| F32 | 已验收并合入 `develop` |
| 当前工作树 | 交接文档写入前干净；写入本文后新增本文档尚未提交 |
| 未修改范围 | `E:\ftre-agent-core`、桌面客户端、Inbox/Queue wire、Session wire、Cordis Kernel |

F31/F32 的主要文档：

- `docs/prd/PRD-F30-llm-service-package.md`
- `docs/prd/PRD-F31-agent-service-boundaries.md`
- `docs/prd/PRD-F32-agent-runtime-service-decoupling.md`
- `docs/execution/EXECUTION-F31-agent-service-boundaries.md`
- `docs/execution/EXECUTION-F32-agent-runtime-service-decoupling.md`
- `docs/execution/matrices/F31-agent-service-boundaries.md`

当前 `docs/TODO.yaml` 的 F30、F31、F32 均为 `done` / `已验收`。F33/F34 的早期规划没有
出现在当前 `develop` 的 TODO 和 PRD 中，历史规划曾保存在 `stash@{0}`，恢复前必须先
检查 stash 内容，不能直接假设文件仍存在。

## 2. 最终架构指导思想

### 2.1 核心原则

ftre 的核心指导思想是：

> 轻内核 + Plugin-first。

Kernel 只负责机制，不负责产品业务：

```text
Kernel
├─ Context / Inject
├─ Plugin 生命周期
├─ Hook 注册与分发
├─ Fiber / Effect 资源清理
└─ 诊断、依赖和启动门禁
```

Kernel 不应该拥有：

```text
Session / Queue / Inbox / AgentLoop / Tool / MCP / Compaction / Command / Channel
```

### 2.2 四个概念不要混用

```text
Service  = 运行时能力，通过 Context 注入；有稳定 key 和公开方法
Plugin   = 生命周期 Owner，负责创建、注册、启停和释放 Service/行为
Hook     = 某个时机的扩展契约，由发布者定义，由 Plugin 监听
Package  = 安装、版本和发布边界，不等于 Service，也不等于每个 Plugin 都必须拆包
```

约束：

- 一个 Service key 只有一个 Provider Owner。
- 一个业务能力只保留一个真实 Owner。
- 跨 Owner 只调用公开 Service 方法，不读取 `.manager`、`.registry`、`.projection` 或 Repository。
- 不新增没有业务价值的 Port、Facade、Coordinator、Service Bag、全局 setter 或兼容别名。
- 生命周期资源必须绑定 Plugin Fiber，并通过 `ctx.effect()` 可逆释放。
- 已存在的旧债务必须登记到后续阶段，不能通过测试 allowlist 伪装成已清理。

## 3. 对话中形成的关键设计决策

### 3.1 Agent 与 Inbox/Queue

用户明确要求 Agent Service 不拥有队列概念：

```text
Channel / Command
        ↓
Inbox Package：admission、pending、claim、steer、worker
        ↓ 交付一条已归一化消息
AgentService.run(InboundMessage)
        ↓
Agent Runtime：active Run / Turn / Hook / Core Agent
```

因此：

- `AgentService` 的唯一数据面输入是 `InboundMessage`。
- `QueueItem`、`pending`、`claim`、`next-turn`、`next-step` 不进入 Agent Runtime 的 Turn 模型。
- Agent Runtime 只维护 active task、取消信号和维护期状态，不创建第二个 worker。
- Inbox 可以独立演进、独立发布；缺少 Inbox 时由对应 Composition 门禁决定是否启动，而不是
  让 Agent Runtime 自己创建 no-op 队列。

用户点击“调整方向”时，插件来源的消息也必须转为用户语义再消费：

```text
plugin message(source=plugin)
        ↓ 用户点击 steer
Inbox 原子更新 source=user、placement=context/steering
        ↓
DB-first 持久化 UserMessage
        ↓
Core 下一次 Reasoning 消费
```

这解决了“Agent 已被消息影响，但刷新后消息从 MessageList 消失”的问题。实现和回归测试：

- `packages/ftre-inbox/src/ftre_inbox/repository.py`
- `packages/ftre-inbox/tests/test_steering_delivery.py`

### 3.2 Channel、MessageBus 和 Session Events

```text
Agent Runtime
    ↓ BusMessage（仅作为已有传输信封）
MessageBusService.publish_outbound()
    ↓
Channel Plugin / ChannelManager
    ↓
WebSocket / Subagent / Cron
```

Session 事实走另一条顺序明确的出口：

```text
SessionEventService.emit()
    1. SessionProjection 持久化
    2. MessageBusService.publish_outbound()
```

不能让 Agent Runtime 直接调用 Channel 或底层 EventBus，也不能让 Session Projection 通过
两个出口重复广播。

### 3.3 Hook 方向

Hook 必须表达清晰的业务时机，发布者和监听者分离：

```text
agent/before-run       Agent Runtime 发布，治理/权限 Plugin 监听
agent/after-run        Agent Runtime 发布，Compaction/维护 Plugin 监听
agent/run-error        Turn Runtime 发布，Host 恢复行为监听
agent/request          ftre-llm 发布，模型选择/Fallback 监听
llm/stream             LLM/Core 调用流扩展
llm/error              Agent Core 发布，Retry/恢复策略监听
agent/stop-decision    Agent Core 发布，Steering/Continuation 监听
system-prompt/assemble SystemPromptService 发布，Prompt Feature 监听
```

不要把 `agent/pre-step`、`agent/step`、`turn-stopping` 等相近但语义不清的 Hook 再重复增加；
新增 Hook 前先确认它是否真的代表一个稳定、可观察、可测试的边界。

## 4. F30：统一 LLM Service

### 4.1 目标与边界

`packages/ftre-llm` 是独立 LLM Service Package，负责：

- `LlmService`、`LlmRequest`、`LlmCallConfig`、`LlmCredentials`；
- Provider Adapter Registry；
- OpenAI Chat Completions 和 OpenAI Responses 适配器；
- 统一 StreamChunk 输出；
- `prepare_call()` / `stream()` 调用句柄；
- `llm/stream`、`agent/request`、`llm/error` 协作边界。

它不负责：

```text
Agent / Session / Inbox / Queue / Compaction / Channel / Client
```

Compaction 和 Session Title 通过 `ctx.llm` 调用，不再各自创建 Provider Handler。

### 4.2 Retry 与 Fallback 语义

```text
一次 LLM attempt 失败
        ↓
Core 发布 llm/error
        ↓
Retry Plugin 决定是否重试
        ↓ Retry 耗尽
Fallback Plugin 才能切换备用模型
```

约束：

- Retry 先于 Fallback。
- 已经输出正文或 Tool Call 的半截流不能无感切换模型。
- Fallback 必须重建完整请求，不能拼接两个模型的半截输出。
- 取消不应触发 Fallback。

主要文件：

- `packages/ftre-llm/src/ftre_llm/service.py`
- `packages/ftre-llm/src/ftre_llm/contracts.py`
- `packages/ftre-llm/src/ftre_llm/adapters/`
- `packages/ftre-llm-recovery/`
- `packages/ftre-llm-fallback/`
- `src/ftre/services/llm/plugin.py`

## 5. F31：Agent Runtime Service 边界基线

F31 在代码审查后被收窄，不负责真实 Runtime 迁移，只负责：

- 记录 AgentLoop/TurnExecutor 的真实依赖矩阵；
- 确认 Service、Plugin、Hook 的唯一 Owner；
- 冻结现有公开 Service 方法和缺口；
- 建立 Fake Service 契约测试；
- 建立“债务数量不得增加”的架构门禁；
- 给 F32 提供精确删除清单。

这次收窄是为了避免 F31 和 F32 重复实现。F31 的主要测试：

- `tests/architecture/test_f31_agent_service_boundaries.py`
- `tests/contracts/test_f31_service_contracts.py`

F31 原来写过的一批虚构 Protocol/DTO（例如 `AgentSessionView`、`AgentEventSink`、
`PromptAssembler`、`EffectiveAgentProfile`）已经删除，不得重新引入同义层。

## 6. F32：Agent Runtime Service 化与真实解耦

### 6.1 当前调用关系

```text
src/ftre/services/agent/plugin.py
        ↓
runtime/provider.py::build_runtime(ctx, agent_service)
        ↓ 只组装公开 Service
runtime/engine.py::AgentLoop
        ↓
runtime/turn_executor.py::TurnExecutor
        ↓
runtime/factory.py::create_core_agent()
        ↓
ftre-agent-core::ReActAgent
```

Provider 当前注入的核心 Service：

```text
message_bus
sessions
tools
workspaces
profiles
config_service
system_prompt
llm_service
hook_runtime
session_events
attachments（可选）
traces（可选）
```

### 6.2 已完成的迁移

- `AgentLoop` 不再持有 `ChannelManager`、MCP Service、全局 `ToolRegistry`、`AgentManager`、
  `SessionProjection`。
- `TurnExecutor` 的输入是 `InboundMessage`，不再将 Host `BusMessage` 作为 Turn 输入。
- Session 上下文、消息 CRUD 和 open reply 收尾通过 `SessionService` 公共方法完成。
- 新增 `SessionService.finish_open_replies()`。
- 出站状态通过 `MessageBusService.publish_outbound()`。
- Tool View、MCP 准备和权限过滤由 `ToolService.prepare_view()` Owner 完成。
- Profile 解析通过 `AgentProfileService.resolve_for_inbound()`。
- Workspace accessor 迁移到 `WorkspaceService`。
- 配置读取通过 `ConfigService.resolve_agent_config()`。
- Core Agent 的唯一 Host 构造点是 `runtime/factory.py`。
- 删除 `src/ftre/services/tools/builtin/_workspace.py`。
- 删除 `AgentManager` 中重复的 Agent 构造、Prompt、权限状态工厂逻辑。
- 修复 LLM `adapters-updated` Hook 回调捕获错误的 Context Locator。
- 修复插件消息 steer 后未持久化为用户消息的问题。

### 6.3 代码落点

| 能力 | 文件 |
|---|---|
| Runtime Provider | `src/ftre/services/agent/runtime/provider.py` |
| AgentLoop | `src/ftre/services/agent/runtime/engine.py` |
| Turn 状态机 | `src/ftre/services/agent/runtime/turn_executor.py` |
| Core 构造 | `src/ftre/services/agent/runtime/factory.py` |
| Loop → AgentService | `src/ftre/services/agent/runtime/driver.py` |
| Tools View | `src/ftre/services/tools/service.py` |
| Workspace 边界 | `src/ftre/services/workspace/service.py`、`accessor.py` |
| Config 边界 | `src/ftre/services/config/service.py` |
| MessageBus 出站 | `src/ftre/services/messaging/bus/service.py` |
| Session 事件出口 | `src/ftre/services/session/events.py` |
| Profile 解析 | `src/ftre/services/agent/profile/service.py` |

## 7. 当前真实数据流

### 7.1 普通用户消息

```text
WebSocket/HTTP Channel
        ↓
InboundMessage
        ↓ Inbox admission / claim
AgentService.run()
        ↓ AgentLoop.run_inbound()
校验 Session/channel
        ↓
resolve_inbound_config()
        ↓ agent/before-run
SessionEventService.emit(UserMessageEvent)
        ↓ 先写 state.json，再广播 USER_MESSAGE
TurnExecutor.execute(InboundMessage)
        ↓
SessionService.get_context_messages()
SystemPromptService.assemble_result()
ToolService.prepare_view()
LlmServiceAdapter → Core ReActAgent
        ↓
SessionEventService.emit(assistant/tool/turn events)
        ↓
MessageBusService.publish_outbound()
        ↓
WebSocket Channel → 客户端
```

### 7.2 Steering 消息

```text
客户端 session.updateQueue(action=steer)
        ↓
Inbox 原子更新 placement/source
        ↓
Inbox 在下一次 Reasoning 边界交付 InboundMessage
        ↓
先持久化 UserMessage，再让 Core 消费
        ↓
客户端收到 USER_MESSAGE，队列项从当前快照消失
```

关键不变量：不能先从 Inbox 删除、后异步写 Session；否则进程崩溃或刷新会造成“Agent 感知了
消息，但 MessageList 没有消息”。

### 7.3 Assistant 生命周期

```text
TURN_START
  ↓
REPLY_START：创建并保存一个 Assistant Msg
  ↓
reasoning/tool/stream event：投影增量并按屏障 checkpoint
  ↓
REPLY_END：写 finished_at、finished_reason、usage 等最终信息
  ↓
TURN_END
  ↓
agent/after-run：Compaction 等维护 Hook
  ↓
发布 idle
```

`agent/after-run` 维护期间，Runtime 通过内部状态标记 `compacting`，防止 Inbox 误领下一条；
压缩实现属于 `ftre-compaction`，不应重新放回 AgentLoop。

## 8. 已验证的测试证据

F32 合并前后报告记录过以下结果：

```text
架构 / 契约 / 启动专项：186 passed
生命周期专项：262 passed
F32 后端全量：629/630/631 passed（随后续安全与 Inbox 修复递增）
Ruff：All checks passed
git diff --check：通过
Gateway smoke：GET /api/health -> 200 {"status":"ok"}
```

当前接任 Agent 必须在任何后续修改后重新执行，不能直接复用历史数字：

```powershell
cd E:\ftre
python -m pytest -q tests/architecture tests/contracts tests/startup tests/lifecycle
python -m pytest -q
python -m ruff check src tests packages --no-cache
git diff --check
```

运行测试会生成 `__pycache__`、`.pytest_cache`、`.ruff_cache` 等忽略文件；它们不能进入提交。

## 9. 已知遗留问题与处理顺序

### P1：先处理安全和工作树卫生

- 任何 `verify_model.py`、`test_all_models.py`、`cross_verify.py` 等临时模型测试脚本不得提交。
- 历史临时脚本曾包含凭据；若再次发现类似文件，先精确隔离/删除，再让密钥所属服务吊销并重发。
- 不要把密钥写入执行报告、交接文档、测试输出或 Git 历史。

### P2：F32 后续边界收紧

- `MessageBusService.bus` 仍被 Channel Owner 用于构造底层 ChannelManager；这不是 Agent Runtime
  直连债务，但未来可进一步收紧 Service 对外暴露面。
- `SessionService.projection`、`AgentProfileService.manager`、`ToolService.registry` 仍是各自
  Owner 内部/历史公开属性；后续清理必须先审计所有消费者，不能只删除属性。
- `AgentLoop` 的部分类型注解仍引用 Host 具体模型；Package 抽取前应迁移到稳定契约或类型检查
  专用导入，不要复制新 DTO。
- 个别 Runtime 注释仍提到已由 Service Owner 管理的 Projection/Manager，修改注释时要与真实
  调用链同步，不能用注释掩盖实际依赖。

### P3：测试门禁增强

- 现有部分架构测试仍包含源文本断言；后续应逐步改成 AST 的属性访问、构造参数和调用图分析。
- 契约测试除了方法名，还应校验签名、返回形状、错误语义和生命周期清理。
- Manifest 测试应确认不同 Manifest 不会指向同一个 Plugin callable，且所有 Service key 的
  Provider 都纳入检查。

## 10. 下一阶段方向

F32 后 Agent Runtime 仍在 Host，尚未抽成 Package。理想终局是：

```text
E:\ftre\
├─ packages/
│  ├─ ftre-agent/                  # 稳定 AgentService、InboundMessage、状态和 Hook 契约
│  ├─ ftre-agent-runtime/          # AgentLoop、TurnExecutor、Driver 的具体实现
│  ├─ ftre-llm/
│  ├─ ftre-inbox/
│  ├─ ftre-compaction/
│  ├─ ftre-llm-recovery/
│  └─ ftre-llm-fallback/
└─ src/ftre/
   ├─ app/gateway/                 # Composition Root
   ├─ services/                    # Host 稳定 Service
   ├─ plugins/builtin/             # Host 行为 Plugin
   └─ kernel/                      # 轻量机制层
```

推荐顺序：

1. 先恢复并审阅 F34 `ToolService` 最终边界 PRD（若仍需要）；
2. 冻结 `ToolService` 的公开方法、Tool Definition、View、执行结果和 Hook 语义；
3. 再恢复/重写 F33 `ftre-agent` Package PRD；
4. 最后把 AgentService 契约和 Runtime 实现分包，保持 Host Composition 只负责装配。

不要把 F33 Package 抽取、Tool Service 重写、Queue wire 改造和客户端协议改造放进同一批。

## 11. 接任 Agent 的开工检查清单

```text
[ ] 阅读本文以及 F30/F31/F32 PRD、执行报告和 TODO
[ ] 确认当前分支、HEAD、工作树和 stash，不覆盖他人修改
[ ] 扫描临时脚本、缓存、凭据和未提交文件
[ ] 明确本阶段的 PRD 已 approved/开发中，不能跳过文档状态
[ ] 确认只修改授权仓库，默认不修改 Core 和客户端
[ ] 先画当前 Owner/Inject/Hook 依赖图，再改代码
[ ] 新增能力优先作为 Plugin，Service 只保留稳定运行时能力
[ ] 不新增同义 Port/Facade/Coordinator/Locator
[ ] 每个行为变更配回归测试和中文边界注释
[ ] 完成 pytest、ruff、diff check 后再提交
```

## 12. 一句话交接结论

F30 已统一 LLM Service，F31 已冻结 Agent Runtime Service 边界，F32 已将 AgentLoop/TurnExecutor
真实改为消费公开 Service；当前最重要的后续不是继续给 Runtime 加字段，而是保持“轻内核 +
Plugin-first”，审阅恢复 F34/F33 规划，并在不恢复 Manager/Projection/Queue 直连的前提下完成
ToolService 和 Agent Package 的最终边界。
