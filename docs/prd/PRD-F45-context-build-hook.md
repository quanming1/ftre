# PRD-F45 Agent Context Build Hook 与压缩策略插件化

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收
>
> 本阶段只处理“本次 LLM 请求使用哪些上下文”的扩展边界。消息持久化、Event/Msg
> 下行协议和 Inbox 队列继续遵循 F44/F12，不在本阶段重新设计。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F45 |
| 名称 | Agent Context Build Hook 与压缩策略插件化 |
| 状态 | 已验收 |
| 创建日期 | 2026-09-07 |
| 定稿日期 | 2026-09-07 |
| 验收日期 | 2026-09-07 |
| 关联文档 | `PRD-F44-session-snapshot-protocol.md`、`PRD-F42-client-session-lifecycle.md`、`PRD-F11-compaction-gate-hook.md`、`PRD-F26-compaction-token-chunks.md`、`docs/PROCESS.md`、`docs/TODO.yaml` |

## 1. 背景与目标

### 1.1 当前问题

F44 已经把 Session 的持久化事实收敛为完整 Msg Snapshot，`assistant/chunk` 只属于
运行时直播和内存聚合。但 Agent Runtime 仍直接把 `AgentState.context` 转成 Provider
消息，`ftre-agent.session.derive` 还包含 fast compact 的上下文裁剪规则。结果是：

1. 压缩算法在 `ftre-compaction`，压缩后的上下文替换却分散在共享 derive/Runtime；
2. `before-reasoning` 既承担 Inbox/Skill 消息追加，又可能被继续扩大为整段上下文替换，
   语义不清、监听器容易互相覆盖；
3. `llm/stream` 已经接近 Provider 流包装边界，才在这里修改上下文会重复执行或太晚；
4. Fork/回滚如果复制“已经裁剪过的上下文视图”，会丢失原始 ToolResult、压缩锚点或
   request 幂等边界。

### 1.2 目标

新增一个稳定的 `agent/context-build` Hook：在每轮 Reasoning 的基础 Msg 上下文准备好
之后、Provider 消息转换和 `llm/stream` 之前，由插件决定本次 LLM 请求看到的上下文；
压缩插件拥有摘要/fast 替换策略，Runtime 只负责生命周期和调用顺序，Session 仍保存
完整原始 Msg。

### 1.3 非目标

- 不修改 LLM Provider、OpenAI Responses/Chat Completions 适配器或 `llm/stream` 契约；
- 不把压缩规则重新放回 Agent Runtime、SessionService 或 Inbox；
- 不删除 `compact/message` 事实事件，也不删除原始 User/Assistant/ToolResult Msg；
- Fork/回滚不另立阶段；本阶段同时交付后端截止接口、Session 隔离和客户端入口，
  但不扩展为通用的历史编辑器或时间旅行系统；
- 不新增第二套 Event→Msg 投影、第二个 Session 历史存储或新的“压缩 Service” Owner。

## 2. 需求范围

### 2.1 功能需求

- [x] **FR1：公开 Hook 契约。** 在 `ftre-agent` 中增加
  `AGENT_CONTEXT_BUILD_SPEC`、`ContextBuildPayload` 和 `ContextBuildResult`。契约
  必须是无 Host 依赖的纯数据模型，默认结果为原样透传。

- [x] **FR2：输入快照。** Payload 提供本轮最终的 `Msg` 序列（已包含
  `agent/before-reasoning` 返回的 Inbox/Skill 消息）、Session/Turn/Request/Iteration
  坐标、模型名、上下文预算和取消信号。Runtime 必须向 Hook 传递深拷贝，监听器不能
  修改 `AgentState.context` 或 Session Snapshot。

- [x] **FR3：明确调用顺序。** 每次 Reasoning 严格遵循：

  ```text
  before-reasoning
      → 把 Inbox/Skill 消息追加到 AgentState.context
      → context-build（只生成本轮内存 ContextView）
      → MessageContext Provider 转换
      → llm/stream
  ```

- [x] **FR4：Retry 不重复构建。** 同一 Reasoning iteration 内的 Provider Retry 必须复用
  已完成的 ContextView，不能因为第 2/6 次重试再次执行摘要或 fast 裁剪。发生
  `agent/run-error` 的新一轮恢复时，才重新执行 `before-reasoning → context-build`。

- [x] **FR5：Hook 失败可控。** Hook 使用 `WATERFALL` + 默认透传；可选插件异常使用
  `OBSERVE` 语义，记录 session/turn/iteration 和插件名后继续使用原始上下文。返回值
  必须通过契约校验，禁止静默接收任意 dict。

- [x] **FR6：压缩插件接管上下文策略。** `ftre-compaction` 注册该 Hook：

  - summary compact：按 `compact/message` 的 `through_message_id` 生成摘要锚点和
    tail ContextView；
  - fast compact：按 `context_compact.tool_result_ids` 在内存副本中替换 ToolResult
    输出为稳定占位文本；
  - 没有对应 marker 时不猜测、不裁剪；旧 marker 只按已冻结的兼容规则处理并记录；
  - 不在每次 Hook 调用中新增 compact Msg，不重复调用压缩 LLM。

- [x] **FR7：持久化事实不变。** `session.json` 始终保留完整 Msg、compact marker、
  原始 ToolResult 和 request 索引；ContextView 只存在本次 Agent 请求的内存生命周期，
  不得写入 SessionService、WebSocket 或客户端历史。

- [x] **FR8：共享 derive 收窄。** `ftre_agent.session.derive` 继续负责 Event→完整 Msg
  的通用 fold；压缩专属的“按 fast marker 替换 ToolResult 输出”和“按摘要锚点切上下文”
  不再作为 Runtime/Session 的上下文 Owner，迁移到 `ftre-compaction` 的 Hook 实现。

- [x] **FR9：无压缩插件仍可运行。** 未安装或未启用 `ftre-compaction` 时，Hook 默认透传，
  Agent 仍能正常调用 LLM；系统不得 import、实例化或要求压缩 Service。

- [x] **FR10：可观测性。** 每次 ContextView 构建输出结构化诊断：session、turn、iteration、
  原始 Msg 数、最终 Msg 数、裁剪 ToolResult 数、压缩 marker id、耗时和插件名；不输出
  API Key、完整用户正文或工具敏感结果。

- [x] **FR11：Fork 截止接口。** `POST /api/sessions/{session_id}/fork` 接收可选
  `through_message_id`，从父 Session 的完整 Snapshot 按消息顺序复制到该 Msg（包含该 Msg）。
  不传时复制当前稳定 Snapshot 全部消息；返回子 Session id、父 Session id、截止消息 id
  和子 Snapshot seq。

- [x] **FR12：Fork 一致性屏障。** Fork 前必须完成当前 Session 的 checkpoint，并确认没有
  active Turn、in-flight compaction、未决 approval 或未闭合 ToolResult；否则返回明确的
  `session_busy`/`fork_target_not_stable`，不复制半成品，不等待后静默重试。

- [x] **FR13：Fork 运行态隔离。** 子 Session 继承消息、workspace、必要的展示 metadata 和
  compact marker，但不继承 Inbox pending、active run、turn、approval、completion waiter；
  子 Session 的 request 幂等索引重新建立，新请求不得复用父 request_id/run_id。

- [x] **FR14：Fork 与压缩兼容。** 子 Session 只复制完整 Msg；summary marker、
  `through_message_id`、fast `tool_result_ids` 和原始 ToolResult 一起保留，由子 Session
  下一次 `agent/context-build` 重新生成 ContextView。不得复制 Provider 消息或 fast 占位文本。

- [x] **FR15：客户端入口。** AI 消息的 Fork 操作调用 `/fork` 创建新 Session；用户消息的
  回滚操作调用独立的 `/rollback` 接口原地改写当前 Session，并将被移除的用户文本重新填回
  输入框（不自动发送、不自动执行父请求）。

### 2.2 非功能需求

- **性能**：Hook 本身不做深度全历史扫描；Compaction Plugin 可使用消息 id/marker 索引。
  同一 iteration 的 Retry 不得重复 O(N) 压缩处理或重复调用压缩 LLM。
- **一致性**：Runtime 与 Compaction 测试必须证明 Hook 前后的原始 `AgentState.context`
  和 `session.json` 字节内容不变。
- **安全**：插件只能看到公开 Payload；不得通过 ContextView 写回 Session、队列或请求幂等
  索引。
- **可卸载**：Hook Receipt、后台压缩任务和缓存均绑定 Plugin Effect；卸载后只剩默认透传。

## 3. Owner 与边界

| 组件 | 负责 | 不负责 |
|---|---|---|
| `ftre-agent` | HookSpec、Payload/Result 类型和默认透传 | 注册监听器、压缩算法、Session I/O |
| `ftre-agent-runtime` | 调用顺序、深拷贝、Retry 复用、结果校验、Provider 转换 | 判断是否压缩、解释 compact marker |
| `ftre-compaction` | 阈值判断、分块、摘要 LLM、fast 裁剪、`context-build` 监听器 | 领取 Inbox、写客户端、拥有 Session 历史 |
| `SessionService` | 完整 Msg/Event Snapshot、原子 checkpoint、Fork 数据源 | 构建某次请求的裁剪 ContextView |
| `InboxService` | pending、claim、投递和队列持久化 | 压缩和上下文裁剪 |
| `LLMService` | Provider 调用和流协议 | Agent 上下文策略 |

## 4. Hook 契约

### 4.1 数据结构

契约示意（最终字段以代码和类型测试为准）：

```python
@dataclass(frozen=True, slots=True)
class ContextBuildPayload:
    session_id: str
    turn_id: str
    request_id: str
    iteration: int
    model: str
    messages: tuple[Msg, ...]       # Runtime 深拷贝后的 Msg 快照
    context_limit: int | None
    cancellation: asyncio.Event


@dataclass(frozen=True, slots=True)
class ContextBuildResult:
    messages: tuple[Msg, ...]       # 本次请求使用的 ContextView


AGENT_CONTEXT_BUILD_SPEC = HookSpec(
    "agent/context-build",
    "agent",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.OBSERVE,
    payload_type=ContextBuildPayload,
    result_type=ContextBuildResult,
    default=_identity_context,
    scope=HookScope.AGENT,
)
```

约束：

1. `messages` 的顺序和 Msg id 必须保留；插件只能删除/替换本次视图中的内容，不能修改
   父 Session 的 Msg；
2. `ContextBuildResult` 不携带 Provider 私有字段、不携带 Event、不携带持久化命令；
3. Hook 不能返回 `None` 表示“随便处理”，无变更必须显式返回输入快照；
4. Hook 只在 Runtime 内部调用，不新增 HTTP/WS 路由。

### 4.2 Runtime 伪代码

```python
# before-reasoning 已经把 Inbox/Skill 的追加消息写入 state.context
base_context = tuple(message.model_copy(deep=True) for message in state.context)
payload = ContextBuildPayload(
    session_id=session_id,
    turn_id=state.turn_id,
    request_id=request_id,
    iteration=state.iteration,
    model=self.agent.model,
    messages=base_context,
    context_limit=self.agent.context_window,
    cancellation=cancellation,
)
result = await hooks.dispatch(AGENT_CONTEXT_BUILD_SPEC, payload, context=hook_context)
AGENT_CONTEXT_BUILD_SPEC.validate_result(result)

# 只把 Hook 结果交给 Provider 转换；state.context 保持完整
provider_messages = MessageContext.get_messages(
    [message.model_copy(deep=True) for message in result.messages],
    self.agent.system_prompt,
)

# 后续 attempt 复用 provider_messages，不重复执行 context-build
```

### 4.3 与现有 Hook 的关系

| Hook | 语义 | 是否替代 |
|---|---|---|
| `agent/before-reasoning` | 追加 Inbox/Skill 等消息 | 不替代 |
| `agent/context-build` | 生成本次 LLM 使用的上下文视图 | 新增 |
| `llm/stream` | 包装一次 Provider 流 | 不替代 |
| `llm/error` | 单次 LLM attempt 错误决策 | 不替代 |
| `agent/run-error` | Agent Run 级恢复/重试 | 不替代 |
| `agent/after-run` | Run 结束后的维护 | 不替代 |

## 5. 压缩插件迁移方案

### 5.1 保留的能力

- `CompactionService` 继续负责压缩触发、chunk、摘要模型调用、共享 Task、取消和进度；
- `compact/message` 仍然是持久化/下行事实，包含摘要锚点、fast 模式和裁剪 id；
- `agent/after-run`、`agent/run-error`、`inbox/before-claim` 继续负责主动门控和 overflow
  恢复；它们不承担本次请求的 ContextView 替换。

### 5.2 迁移步骤

1. 在 `ftre-agent/hooks.py` 冻结 `agent/context-build` 契约和 golden fixture；
2. Runtime 在 `ReasoningExecutor` 的 Retry 循环外构建一次 ContextView；
3. Compaction Plugin 增加监听器，把现有 fast/summary 上下文逻辑迁入插件内部；
4. 将共享 `derive.py` 中仅服务于 LLM 上下文的压缩替换逻辑删除或降为插件私有 helper，
   保留完整 Msg/Event fold；
5. 清理 Runtime 对 `derive_context_messages_*` 的直接调用和任何“是否压缩”的条件分支；
6. 增加未安装压缩包、Hook 异常、Retry 复用、卸载恢复透传的测试。

## 6. Fork/回滚实现方案

Fork 最容易出错的地方不是复制 UI，而是把“完整历史”和“本次请求视图”混在一起。必须
遵守以下规则：

### 6.1 `through_message_id` 截止协议

F45 的 Fork API 使用可选的 `through_message_id` 指定复制截止点：

```http
POST /api/sessions/{session_id}/fork
Content-Type: application/json
```

```json
{
  "through_message_id": "assistant_123"
}
```

字段语义：

- `through_message_id` 是父 Session 中一条完整 Msg 的 `id`，不是 `seq`、`cursor` 或
  事件下标；
- 服务端从完整 Snapshot 中按原顺序复制该消息及其之前的消息（包含截止消息）；
- 不传该字段表示复制当前稳定 Snapshot 的全部消息；
- ID 不存在、指向隐藏/未完成的 chunk、运行中的 Tool 或未决审批时，返回明确错误，
  不能猜测最近一条消息；
- 返回值至少包含 `fork_session_id`、`parent_session_id`、`through_message_id` 和
  子 Session 的 Snapshot `seq`；
- F45 需要同步修改后端路由和客户端 API；旧的无请求体调用保持“复制全部稳定 Snapshot”
  的兼容语义。

### 6.2 回滚接口与操作语义

回滚不是 Fork 的一种模式，而是当前 Session 的历史改写。接口固定为：

```http
POST /api/sessions/{session_id}/rollback
Content-Type: application/json
```

```json
{
  "through_message_id": "user_123"
}
```

服务端在 Snapshot 屏障后，以原子提交方式移除目标 user Msg 及其之后的 Msg，保留当前
`session_id`、metadata、workspace 和未受影响的历史；同时清理被移除消息对应的 request
幂等索引，并返回 `prefill_content`。客户端重新拉取当前 Session 的 HTTP Snapshot 后，将
`prefill_content` 放入输入框，不创建子 Session、不修改其它 Session、不自动重跑副作用。

目标必须是稳定的 user Msg；运行中的 Turn、压缩、审批、队列或未闭合 Tool 状态返回
`session_busy`/`rollback_target_invalid`，不做部分改写。

### 6.3 Fork 与回滚的操作语义

| 操作 | 输入 | 结果 | 是否自动执行 |
|---|---|---|---|
| Fork AI 消息 | `through_message_id=assistant_xxx` | 创建新 Session，复制截止消息及之前的完整 Msg | 否 |
| 回滚用户消息 | `through_message_id=user_xxx` | 原地删除当前 Session 中该用户消息及之后的 Msg，并将该用户文本填回输入框 | 否 |
| 全量 Fork | 不传截止 id | 创建新 Session，复制当前稳定 Snapshot 全部 Msg | 否 |

回滚直接改变当前 Session；它与 Fork 的区别是没有新 Session、没有父子关系、没有复制历史。
若需要保留原分支，应先使用 Fork，再在新 Session 中继续工作。

1. **Fork 数据源只能是完整 Msg Snapshot。** 禁止从 `ContextBuildResult`、Provider 消息、
   fast 占位文本或客户端当前列表复制；
2. **先取得一致性屏障。** 父 Session 正在运行 Turn、压缩、审批或 checkpoint 未完成时，
   只能返回明确的 `session_busy`，不能复制半条 Assistant、未闭合 ToolResult 或旧快照；
3. **压缩摘要按事实复制。** 若复制范围包含 summary `compact/message`，保留 marker、
   `through_message_id` 和 `context_compact` 元数据；不要把摘要展开成新消息，也不要把
   原始历史物理删除；
4. **fast 压缩只复制 marker 和原始结果。** 子 Session 下一次执行时由自己的
   `context-build` 再应用 `tool_result_ids`，绝不能把 `[已压缩裁剪]` 占位文本当成真实历史；
5. **Fork 截止点必须是 Msg 边界。** 选中的消息若仍只有 chunk、Tool 正在运行或审批未决，
   拒绝操作；不能凭半成品 Event 猜测边界；
6. **Inbox 和运行态不继承。** 子 Session 的 pending、active run、turn、approval、
   completion waiter 全部为空；父 Session 不得被修改；
7. **幂等索引重新建立。** 子 Session 不能直接复用父的 `request_id → run_id` 活跃索引。
   复制的历史消息可保留 `source_request_id` 作为溯源，但新请求必须使用新 request_id，
   避免被错误去重或恢复执行；
8. **seq 重新定义。** 子 Session 有自己的 seq 水位和事件流；复制的历史 Msg 不得让子
   Session 误以为父事件仍可在本地重放。新事件从子快照的基线之后继续递增；
9. **压缩不改变回滚语义。** 回滚到 compact marker 之前，应从完整历史重建；回滚到 marker
   之后，保留该 marker 及其覆盖范围，不能只保留已经裁剪的 ContextView；
10. **不自动重跑副作用。** Fork 不自动重新执行父 Session 已经执行过的 Tool、MCP、Bash
    或 LLM 请求；回滚也只改写当前历史并回填输入框，等待用户主动发送。

## 7. 目标文件与边界

### 7.1 本阶段预计修改

```text
E:/ftre/packages/ftre-agent/src/ftre_agent/hooks.py
    新增 ContextBuildPayload / ContextBuildResult / AGENT_CONTEXT_BUILD_SPEC

E:/ftre/packages/ftre-agent-runtime/src/ftre_agent_runtime/executors/reasoning.py
    在 Retry 循环外调用 context-build，缓存本轮 ContextView

E:/ftre/packages/ftre-compaction/src/ftre_compaction/hooks.py
E:/ftre/packages/ftre-compaction/src/ftre_compaction/service.py
    注册 Hook，迁移 summary/fast 的上下文变换和诊断

E:/ftre/packages/ftre-agent/src/ftre_agent/session/derive.py
    删除压缩专属上下文替换 Owner，保留完整 Msg/Event fold

E:/ftre/src/ftre/services/session/service.py
E:/ftre/src/ftre/services/session/router.py
    Snapshot 屏障、Fork 截止复制、原地 rollback、request 索引清理

E:/binn/ftre-desktop/packages/renderer/src/services/api.ts
E:/binn/ftre-desktop/packages/renderer/src/features/chat/
    AI 消息 Fork、用户消息回滚、输入框回填和 session 切换

E:/ftre/packages/ftre-agent-runtime/tests/
E:/ftre/packages/ftre-compaction/tests/
E:/ftre/tests/contracts/
    Hook、重试、压缩、持久化不变和生命周期测试
```

### 7.2 明确不修改

- `ftre-llm` Provider 适配器和 `llm/stream`/`llm/error` 协议；
- `ftre-inbox` 队列、claim、pending 和恢复；
- F44 的 Event/Msg/WS attach 基础协议；Fork 只复用 HTTP Session 操作，不新增 wire 帧；
- Provider 侧的消息重放和 Tool 副作用补偿机制。

## 8. 分阶段实施与验收

### P0：契约冻结（已完成）

- 增加 Hook 类型、默认透传、不可变/深拷贝测试；
- 添加含 summary compact、fast compact、ToolResult、运行中 Assistant 的 golden fixture；
- 验收：无监听器时行为与当前一致；Hook 结果类型错误会被拒绝。

### P1：Runtime 接入（已完成）

- 在 ReasoningExecutor 中接入 Hook；ContextView 在 Retry 循环外生成并复用；
- 验收：一次 Reasoning 的 6 次 Provider attempt 只调用一次 context-build；新的
  `agent/run-error` 恢复会刷新完整 Snapshot 后重新构建；`AgentState.context` 不被修改。

### P2：Compaction Plugin 迁移（已完成）

- summary/fast 上下文替换全部由 `ftre-compaction` Hook 完成；
- 清除共享 derive/Runtime 中的压缩策略分支；
- 验收：压缩前后 `session.json` 完全保留原始 Msg；LLM 请求使用正确的摘要/占位视图；
  没有压缩包时正常运行。

### P3：Fork 实现与回归（已完成）

- 实现 `through_message_id`、Snapshot 屏障、AI Fork、原地用户回滚和输入框回填；
- 验收：从父 Session 创建子 Session 后，父子消息独立；子 Session 首次 LLM 请求由
  自己的 context-build 重新生成视图；不存在父的占位文本、Inbox pending 或 active request；
  回滚保持当前 Session id、删除目标之后的历史且不自动重跑父 Tool/LLM；Fork 才创建独立子 Session。

### P4：收尾（已完成）

- 删除旧上下文替换引用、兼容入口和空 helper；
- 运行后端 `pytest`、`ruff`、`git diff --check`，并执行包级独立安装测试；
- 更新 `docs/prd/README.md`、`docs/TODO.yaml`、CHANGELOG（实现完成时）。

## 9. 验收标准

- [x] AC1：`agent/context-build` 契约有独立类型、默认透传和 golden fixture；
- [x] AC2：Hook 执行时机位于 `before-reasoning` 之后、Provider 转换之前；
- [x] AC3：同一 iteration 的 Retry 不重复执行 Hook 或压缩 LLM；
- [x] AC4：summary/fast 均只修改内存 ContextView，持久化 Msg/ToolResult 不变；
- [x] AC5：Compaction Plugin 卸载后 Agent 自动恢复原始上下文透传；
- [x] AC6：Hook 异常不会丢消息、不会写入半成品 Session，且有诊断日志；
- [x] AC7：`through_message_id` 按 Msg 边界复制，未完成目标、忙碌 Session 返回明确错误；
- [x] AC8：AI Fork 创建独立子 Session；用户回滚原地改写当前 Session，父/当前 Session
      身份符合各自语义，Inbox/active run 均不被复制，客户端能刷新历史并正确回填输入框；
- [x] AC9：Fork/回滚相关测试证明 compact marker、fast 原始 ToolResult 和 request 幂等
      不被破坏，子 Session 首次请求由自己的 context-build 重新生成视图；
- [x] AC10：后端、客户端全量测试、Ruff、TypeScript、架构扫描和包级安装验证通过。

## 10. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-09-07 | 初稿：新增 `agent/context-build`，明确压缩插件与 Runtime/Session 边界，并冻结 Fork/回滚兼容规则 | 将压缩策略从共享上下文派生逻辑收口到 Plugin，避免复制已裁剪 ContextView 导致历史和幂等损坏 |
| 2026-09-07 | 将 `through_message_id`、AI Fork、用户回滚、Snapshot 屏障和客户端入口纳入 F45，不再另立 F46 | Fork 必须和压缩 ContextView/完整 Msg 的边界一起交付，避免后续再次拆分职责 |
| 2026-09-07 | 完成 Hook、压缩 Plugin ContextView、Snapshot Fork/回滚及客户端入口；删除共享 derive 的压缩裁剪实现并通过全量验证 | 让完整 Msg 成为唯一事实，所有请求级裁剪和分支操作遵循同一边界 |
| 2026-09-07 | 修复打包运行时的 Prompt Hook 输入边界：SystemPromptService 将 typed `Msg` 深拷贝为 JSON mapping 后再交给既有监听器，并增加回归测试 | F45 Runtime 内部改用 typed `Msg` 后，标题插件仍按 `message.get(...)` 读取；统一 Host Hook 输入格式，避免首轮 Turn 在 `_build` 阶段失败 |
| 2026-09-08 | 修正回滚语义：回滚改为独立 `/rollback` 原地截断当前 Session，`/fork` 仅负责创建新 Session；同步 request 索引、客户端刷新和回填测试 | 回滚与 Fork 是两种不同操作，不能通过 `fork(mode=rollback)` 混用 |
