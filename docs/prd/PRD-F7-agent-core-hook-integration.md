# PRD-F7-Agent Core 直接消费 ftre Hook 协议与 Turn-stopping Continuation

> F6 已在 Gateway 内建立 Cordis-backed `HookRuntime`，但 `ftre-agent-core` 仍维护一套
> 独立的 `FtreCoreHookManager`。F7 只把 Agent Core 真正需要的少量算法 Hook 接到同一份
> Core-facing 契约上；Session、Mailbox、Compaction 和 Plugin 生命周期 Hook 继续归 ftre。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F7 |
| 名称 | Agent Core 直接消费 ftre Hook 协议与 Turn-stopping Continuation |
| 状态 | 已验收 |
| 创建日期 | 2026-08-21 |
| 定稿日期 | 2026-08-21 |
| 验收日期 | 2026-08-21 |
| 关联文档 | `docs/TODO.yaml` 阶段 F7；`AGENTS.md`；`docs/PROCESS.md`；`PRD-F6-semantic-hook-system.md` |
| 外部协作 | `E:\ftre-agent-core` 以 C1 独立 PR 实现 Core 协议；两个仓库分别验证，不在本仓库内复制 Core 实现 |

## 1. 背景与目标

### 1.1 当前问题

Ftre Gateway 已经拥有类型化 `HookSpec`、Cordis-backed `HookRuntime`、Agent Scope、Fiber
生命周期和诊断能力，但 Agent Core 仍拥有另一套算法层 Hook：

```text
ftre HookRuntime
  → ToolHookBridge
  → ftre-agent-core FtreCoreHookManager
  → HookedToolRegistry / _HookedTool
  → ReAct ToolHandler
```

LLM stream 又通过 `HookedLLMAdapter` 另外包装。结果是同一个 Tool/LLM 调用存在两套
Hook 注册与结果转换，Hook 语义、错误策略和生命周期容易漂移。

另外，当前 ftre `agent/turn-stopping` 在 `TurnExecutor._drive()` 的 `finally` 阶段触发，
此时 Agent 已经进入终态，只能观察，不能阻止停止并进入 continuation。

### 1.2 目标

让 Agent Core 在算法边界直接消费 Core-facing ftre Hook 契约，由 ftre 的 `HookRuntime`
作为唯一运行时实现；删除重复的 Core HookManager、Tool Registry 包装和 LLM 转换层，
并把 `agent/turn-stopping` 升级为有界的停止决策 Hook。

### 1.3 非目标

1. 不把全部 ftre Hook 迁入 Agent Core；Session、Mailbox、Compaction、Plugin 生命周期、
   Session flush/created/disposed 等仍属于 Gateway。
2. 不让 `ftre-agent-core` 直接 import `ftre.kernel.hooks` 或 Cordis，避免反向依赖和
   包循环；Core 只依赖无 ftre 业务状态的 Core-facing 契约。
3. 不修改 Desktop、WebSocket wire 协议、Session JSON、Agent Core 以外的客户端或仓库。
4. 不把 ftre/Cordis 依赖反向引入 `E:\ftre-agent-core`；Core 改动必须在 Core 自己的
   C1 分支与 PR 中完成，两个仓库仍保持独立提交与发布边界。
5. 不在本阶段实现通用任务编排、无限 continuation、跨 Session steer 或远程插件安装。

## 2. 需求范围

### 2.1 功能需求

- [x] **FR1：单一 Core-facing Hook 契约。** 将 Agent Core 需要的 `HookSpec`、调度模式、
  失败策略、Core payload/result 和 `HookDispatcher` Protocol 提取为无 ftre 业务依赖的
  canonical contract。ftre `HookRuntime` 和 Agent Core 使用同一份类型，不复制第二套定义。

- [x] **FR2：选择性迁移 Hook。** 第一批只纳入 `tools/pre-execute`、`tools/execute`、
  `tools/post-execute`、`tools/result`、`llm/stream` 和 `agent/turn-stopping`；
  `agent/pre-step`、`agent/request`、`agent/request-error`、Session、Compaction、Prompt
  及 Mailbox Hook 保持 ftre Owner。

- [x] **FR3：Agent Core 直接注入 Dispatcher。** `ReActRunner`、`ToolHandler` 和停止决策
  边界接收可选 `HookDispatcher` 与不透明 scope context，直接调用 canonical Spec；Core
  不再创建或持有独立 Hook 注册表。

- [x] **FR4：删除 Tool 转换层。** ToolHandler 在原始 Tool 调用前后直接 dispatch
  Core-facing Tool Hook；删除 `ToolHookBridge`、`HookedToolRegistry` 和 `_HookedTool`，
  不复制 Registry、不改变 Tool 原始身份。

- [x] **FR5：删除 LLM 转换层。** Core 的 LLM stream 边界直接以 `LLMStreamPayload.invoke`
  作为 waterfall continuation；删除 ftre `HookedLLMAdapter`，保留原始 LLM Adapter。

- [x] **FR6：Turn-stopping 升级为停止决策。** `agent/turn-stopping` 在 Agent Core 正常
  完成、finalize 之前触发，返回 `StopTurn` 或 `ContinueTurn`；不再把它实现为只能观察的
  `SERIAL` 收尾通知。

- [x] **FR7：Continuation 有界执行。** `ContinueTurn` 必须包含非空 prompt；每个 Turn
  具有 `continuation_count/max_continuations`，达到上限、取消、异常或超最大迭代次数时
  不得继续；同一个 `request_id/turn_id` 内完成 continuation，不写入 mailbox。

- [x] **FR8：停止前后语义分离。** 新增 `agent/turn-stopped` 作为完成后的 `EMIT` 观察 Hook；
  `agent/turn-stopping` 只承担停止前决策，避免一个名字同时表达两个时机。

- [x] **FR9：错误与取消策略保持一致。** Core-facing 控制 Hook 遵循 `PROPAGATE`；观察型
  Result/Stopped Hook 遵循 `OBSERVE`；取消在 dispatch 前后均检查，不得遗留活动 Task 或
  重复执行 Tool 副作用。

- [x] **FR10：ftre 运行时仍是唯一实现。** Core 只消费 Dispatcher Protocol；Plugin 注册、
  Fiber Effect、Agent Scope、Cordis 调度、诊断和 unload/restart 继续由 ftre `HookRuntime`
  负责。

- [x] **FR11：旧入口清零。** 生产代码不再 import `ftre.infrastructure.agent_core` 中的
  Hook 转换器，不再 import `ftre_agent_core.hooks.FtreCoreHookManager`；旧模块在 Core
  侧完成独立迁移后删除，不保留长期兼容壳。

- [x] **FR12：外部仓库边界可验收。** ftre 与 `E:\ftre-agent-core` 分别在各自 feature
  分支维护实现与测试；Core 保持无 ftre/Cordis 依赖，ftre 只通过已安装的 Core 公共协议消费。

### 2.2 非功能需求

- **依赖方向**：`ftre-agent-core → Core-facing contracts`，`ftre → contracts + Core`；
  不允许 `ftre-agent-core → ftre` 或 Core import Cordis。
- **单一事实源**：同一 Core Hook 的 Spec、Payload、Result 只有一份定义；ftre 的 Runtime
  是唯一调度实现。
- **顺序确定性**：同一 Agent Scope 内 Hook 顺序、continuation 次数和停止结果可测试、可诊断。
- **生命周期安全**：Hook unload/restart、in-flight 调用、取消和 continuation 结束均可收敛。
- **行为保持**：无 Listener 时 Agent Core、Tool、LLM 和 Gateway 行为与迁移前一致。
- **安全边界**：continuation 不绕过 SessionLane、Mailbox admission、Tool 权限或取消机制。

## 3. 技术方案

### 3.1 Owner 与依赖方向

```text
ftre-agent-core
└─ hooks/contracts.py
   ├─ HookSpec / HookMode / HookFailurePolicy
   ├─ HookDispatcher Protocol
   ├─ Tool / LLM / Stop Decision Payload & Result
   └─ 不依赖 ftre、Cordis、Session 或文件系统

ftre
├─ platform/hooks/runtime.py       # Cordis HookRuntime，实现 Dispatcher
├─ services/tools/hooks.py         # Tool 语义 Spec 与 Plugin 公共契约
├─ services/llm/hooks.py           # LLM stream 语义契约
├─ services/agent/hooks.py         # Agent 状态与 stop decision 契约
└─ services/agent_loop/             # 在算法边界注入并触发 Dispatcher
```

Canonical contract 已落在 `E:\ftre-agent-core` C1 独立 feature 分支；ftre 不新增
第三套协议，也不把 Cordis Runtime 下沉进 Core。

### 3.2 Core 直接消费方式

```python
agent = ReActAgent(
    ...,
    hooks=hook_dispatcher,
    hook_context=agent_scope_context,
)
```

Tool、LLM 和停止决策均调用同一个 Dispatcher：

```python
result = await hooks.dispatch(
    TOOLS_PRE_EXECUTE_SPEC,
    payload,
    context=hook_context,
)
```

`context` 对 Core 是不透明对象；ftre Runtime 将其解释为 Cordis Scope Context。Core
不读取 `Context`、`Fiber`、Session 或 Agent Registry 字段。

### 3.3 Turn-stopping 数据流

```text
LLM 返回正常完成
  → Core 构造 TurnStoppingPayload
  → dispatch(agent/turn-stopping)
      ├─ StopTurn
      │   → finalize Reply/Turn
      │   → emit agent/turn-stopped
      └─ ContinueTurn(prompt)
          → 写入 Agent Core 内部 continuation hint
          → continuation_count + 1
          → 继续当前 ReAct Turn
```

Continuation 是当前 Turn 内部控制流，不创建新的 QueueItem，不触发新的 admission，
不产生第二个 `request_id` 或 `turn_id`。

### 3.4 删除范围

本阶段已完成 Core 侧独立迁移并通过 ftre 回归，删除：

- `src/ftre/infrastructure/agent_core/tool_adapter.py` 中的 `ToolHookBridge`、
  `HookedToolRegistry`、`_HookedTool`；
- `src/ftre/infrastructure/agent_core/model_adapter.py` 的 `HookedLLMAdapter`；
- `ftre-agent-core` 的 `FtreCoreHookManager`、旧 `HookInput/HookOutput` 和 `ON_*` 注册入口；
- 相关旧适配测试和仅为兼容而存在的导出。

## 4. 接口定义

### 4.1 Dispatcher Protocol

```python
class HookDispatcher(Protocol):
    async def dispatch(
        self,
        spec: HookSpec,
        payload: object,
        *,
        context: object | None = None,
    ) -> object: ...
```

### 4.2 Stop Decision

```python
@dataclass(frozen=True, slots=True)
class StopTurn:
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ContinueTurn:
    prompt: str
    reason: str = ""
    source: str = ""
```

### 4.3 Stop Payload

```python
@dataclass(frozen=True, slots=True)
class TurnStoppingPayload:
    agent: object
    session_id: str
    turn_id: str
    request_id: str
    last_assistant_text: str
    finish_reason: str
    iteration: int
    continuation_count: int
    max_continuations: int
    cancellation: asyncio.Event
```

### 4.4 公开 Hook 选择

| Hook | Core 是否直接调用 | 模式 | 结果 |
|---|---:|---|---|
| `tools/pre-execute` | 是 | WATERFALL | allow / deny / arguments |
| `tools/execute` | 是 | WATERFALL | ToolExecutionResult |
| `tools/post-execute` | 是 | WATERFALL | ToolExecutionResult |
| `tools/result` | 是 | EMIT | None |
| `llm/stream` | 是 | WATERFALL | AsyncIterator |
| `agent/turn-stopping` | 是 | WATERFALL | StopTurn / ContinueTurn |
| `agent/turn-stopped` | 否（ftre emit） | EMIT | None |
| `agent/pre-step` | 否 | ftre | EnterStep / RejectStep |
| `session/flush` | 否 | ftre | persistence barrier |
| `compaction/*` | 否 | ftre | Compaction contract |

## 5. 验收标准

- [x] **AC1：依赖方向通过。** Core 不 import `ftre`、`cordis`、Session 或 Gateway 私有模块；
  ftre 与 Core 使用同一份 Core-facing Spec/Payload/Result 定义。
- [x] **AC2：无重复 Hook Manager。** Agent Core 只接受一个 Dispatcher，不再创建
  `FtreCoreHookManager` 或第二套 Hook 注册表。
- [x] **AC3：Tool 直接管线。** Tool pre/execute/post/result Hook 均能命中 ftre Runtime；
  ToolRegistry 不被复制、包装或改变原始 Tool 身份。
- [x] **AC4：LLM 直接管线。** `llm/stream` 能修改/观察原始 stream；不再创建
  `HookedLLMAdapter`。
- [x] **AC5：普通停止保持行为。** 没有 Listener 时 Agent 正常结束，Reply、Session、
  `PIPELINE_END` 和 CompletionRegistry 结果与迁移前一致。
- [x] **AC6：停止决策可继续。** Listener 返回 `ContinueTurn` 时，Core 注入内部 prompt
  并继续同一个 Turn；不会生成新的 pending、request_id 或 turn_id。
- [x] **AC7：停止决策可结束。** Listener 返回 `StopTurn` 或无 Listener 时，Core finalize
  并发出 `agent/turn-stopped`。
- [x] **AC8：Continuation 有界。** 覆盖空 prompt、达到上限、取消、Core error、超最大迭代
  和重复 Hook 调用；任何情况都不会无限循环或重复执行已完成 Tool。
- [x] **AC9：Hook 失败策略正确。** 控制 Hook 失败进入明确错误路径；观察 Hook 失败只记录
  diagnostics，不污染 Turn 事实。
- [x] **AC10：Scope 与生命周期正确。** Agent Scope listener 能命中正确 Agent；Fiber
  unload/restart 后旧 listener 不再生效；in-flight 调用正常收敛。
- [x] **AC11：旧转换层删除。** ftre 生产代码不再依赖 `infrastructure.agent_core` Hook
  Adapter，Core 旧 HookManager/ON_* 入口删除或在其独立仓库完成退役。
- [x] **AC12：完整回归通过。** ftre 全量 pytest、架构/契约/生命周期专项、ruff、YAML、
  diff check 和 Gateway smoke 全部通过；Core 独立测试和安装验证通过。

## 6. 测试计划

### 6.1 Agent Core 独立测试（`E:\ftre-agent-core`）

- Dispatcher 注入、缺省 Dispatcher 和 Hook 无 Listener 行为。
- Tool 四阶段直接 dispatch、参数替换、结果替换、异常和取消。
- LLM stream waterfall、异步 iterator、stream 失败。
- `agent/turn-stopping` 的 Stop/Continue 分支、continuation 次数和最大迭代边界。
- Core 不 import ftre/Cordis 的静态依赖门禁。

### 6.2 ftre 测试

- `tests/contracts/`：共享 Spec/Payload/Result 身份、模式和返回类型。
- `tests/hooks/`：Cordis Runtime 对 Core-facing Hook 的 waterfall/emit、scope 和诊断。
- `tests/lifecycle/`：Hook Fiber unload/restart、in-flight、取消和 continuation 清理。
- `tests/architecture/`：禁止 `FtreCoreHookManager`、`ToolHookBridge`、
  `HookedToolRegistry`、`HookedLLMAdapter` 重新进入生产路径。
- `tests/integration/` 或现有 Agent/Turn 测试：正常完成、继续执行、停止、错误、取消和
  Session 历史一致性。

### 6.3 手动验证

1. 启用一个 Plugin，在 `agent/turn-stopping` 返回 `ContinueTurn`，确认 Agent 继续执行。
2. 配置 `max_continuations=1`，确认第二次停止不再继续。
3. 在 continuation 期间取消 Turn，确认没有新增 pending、没有孤儿 Task。
4. 关闭/重启 Hook Fiber，确认旧 Listener 不再触发。

## 7. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-21 | 初始草稿：选择性 Core Hook 直连、删除转换层、增强 turn-stopping | F6 HookRuntime 已稳定，需要消除 Agent Core 重复 Hook 管线 |
| 2026-08-21 | 开始迁移：`turn-stopping` 改为 Stop/Continue waterfall，新增 `turn-stopped` 观察 Hook，并先以 Bridge 验证停止决策语义 | Core 独立 PR 尚未完成，先冻结 ftre-facing 语义 |
| 2026-08-21 | 根据用户授权，允许本轮在 `E:\ftre-agent-core` 的 C1 feature 分支直接实现协议；两仓库分别验证后删除 ftre 桥接层 | 消除重复 Hook Owner，完成 F7 的真实迁移 |
| 2026-08-21 | F7 验收完成：Core Dispatcher、Tool/LLM 直连、Turn-stopping continuation、架构门禁和双仓库质量门禁全部通过 | 实现目标并关闭重复 Owner |
