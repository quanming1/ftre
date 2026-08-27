# PRD-F11 上下文压缩门控 Hook 化与 SessionLane 解耦

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F11 |
| 名称 | 上下文压缩门控 Hook 化与 SessionLane 解耦 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-22 |
| 定稿日期 | — |
| 验收日期 | 2026-08-22 |
| 关联文档 | `docs/TODO.yaml` 阶段 F11；`PRD-F6-semantic-hook-system.md`；`PRD-F10-compaction-service-owner.md`；`AGENTS.md`；`docs/PROCESS.md` |

## 1. 背景与目标

### 1.1 当前问题

F10 已经将压缩实现收敛到 `services/compaction/CompactionService`，并将
`plugins/builtin/compaction` 作为 Hook Plugin。但 Agent 数据面仍然由
`SessionLane` 直接持有 `ContextGate`，并在 Lane 内决定压缩时机、更新
`CompactOperation`、执行压缩和处理失败：

```text
SessionLane
  → ContextGate.before_claim()
  → CompactionService.should_compact()
  → SessionLane._compact_or_block()
  → ContextGate.compact()
```

同时，现有压缩 Hook 又通过 `agent/pre-step` 进行一次压力检查，
导致主动压缩策略分散在 Lane、ContextGate 和 Feature Hook 三处。当前实现可以工作，
但存在以下架构债务：

1. Agent 数据面知道压缩阈值和压缩失败策略，违反“AgentLoop 只负责状态机”的边界；
2. `before_claim` 与 `agent/pre-step` 都可能触发压缩，策略重复且难以判断 Owner；
3. 轮后压缩没有独立的控制型 Hook，无法让可选压缩包完整拥有主动压缩策略；
4. 压缩失败、Hook 失败、SessionLane 内部错误的状态语义混在同一个 Lane 分支中；
5. 如果直接删除 Lane 的压缩调用，可能破坏 pending 保留、同 Session 串行、客户端
   `compacting/blocked` 状态和关闭时取消 in-flight 压缩等既有不变量。

### 1.2 目标

将“压缩策略和触发”迁移到可选 `ftre-compaction` Hook，同时保留 SessionLane 对
队列串行化和状态机安全边界的所有权：

```text
SessionLane
  → agent/pre-step Hook（领取前门控）
  → claim
  → TurnExecutor
  → agent/after-turn Hook（轮后门控）
  → 下一条 pending

ftre-compaction
  → 80% 强制压缩
  → 70% 轮后预压缩
  → overflow recovery

ftre-compaction.CompactionService
  → token 水位计算、LLM 摘要、快速裁剪、任务并发与取消
```

完成后，`SessionLane` 不再直接 import 或调用 `CompactionService`，也不再拥有
压缩阈值策略；它只负责在 Hook 完成前不 claim、在失败时保留队首、保证单 Session
串行和完成生命周期清理。

本阶段的更高目标是把压缩做成一个真正可选、可独立安装的能力包：

```text
ftre 核心
  ├─ 永远提供 agent/pre-step / agent/after-turn 契约
  ├─ 未安装压缩包时正常运行
  └─ 不 import、不构造、不要求 compaction Service

ftre-compaction（可选发行物）
  ├─ CompactionService
  ├─ 压缩 Hook 注册
  ├─ /compact、/compress-fast Consumer
  ├─ 摘要、快速裁剪、阈值和失败恢复
  └─ 可独立构建、安装、卸载和发布
```

### 1.3 非目标

- 不修改 Desktop、Web 客户端、WS payload 或现有 Session 持久化格式；
- 不修改 `CompactionService` 的摘要 Prompt、token 估算、快速裁剪算法和事件名称；
- 不修改 `ftre-agent-core`，`agent/after-turn` 属于 ftre-only Hook，不下沉 Core；
- 不新增 `CompactionPort`、Adapter、Facade 或第二个压缩 Service；
- 不把 `SessionLane` 变成通用事件总线，也不让 Hook 直接操作 MailboxStore；
- 不删除 `agent/request-error`，overflow 恢复仍由 `ftre-compaction` Hook 负责；
- 不在本阶段处理 F6.12 的 cordis-py PyPI 发布和洁净安装。
- F11 要求完成可独立构建的压缩发行物和可选安装验证，但不要求在没有账号/Token 的
  环境中自动上传 PyPI；正式上传可作为发布操作单独执行。

## 2. 设计原则与不变量

### 2.1 所有权边界

| 责任 | Owner | 说明 |
|---|---|---|
| pending 持久化、peek、claim、取消 pending | `MailboxStore` / `SessionLane` | Hook 不得直接修改队列 |
| 同 Session 只允许一个 Turn 或维护操作 | `SessionLane` | Hook 执行仍受 Lane worker 串行化约束 |
| 何时需要压缩、使用哪个阈值 | `ftre-compaction` | 通过 Hook 读取公开配置和自身 Service |
| 如何计算 token 水位 | `ftre-compaction.CompactionService` | 只读判断，不调 LLM |
| 如何执行压缩、共享 Task、取消和事件 | `ftre-compaction.CompactionService` | 可选包内唯一真实实现 |
| overflow 后是否有进展并重试 | `agent/request-error` Hook | 使用 progress token，最多有界重试 |
| 对外状态快照和 blocked 原因 | `SessionLane` | 保持现有客户端协议不变 |

### 2.2 必须保持的不变量

1. `agent/pre-step` 成功返回 `EnterStep` 前，队首消息仍属于 pending；
2. 压缩等待期间不能 claim 队首，也不能让后续 pending 越过队首；
3. 压缩失败时队首消息不丢失、不重复消费，并可通过取消 pending 或新 admission
   恢复 Lane；
4. 同一个 Session 的 Turn、自动压缩、手动压缩不能并发写入同一份上下文；
5. Hook Plugin unload/restart 不得留下监听器、压缩 Task 或旧 Service 引用；
6. Lane close 必须等待或取消属于该 Session 的真实压缩 Task，不能在 Session 删除后
   继续写入旧 Session；
7. `agent/turn-stopping` 仍表示 Agent Core 的停止决策，不得复用为轮后压缩 Hook；
8. Command（包括 `/compact`）仍绕过 Agent Turn，使用公开 `CompactionService`。

## 3. 功能需求

### 3.1 新增轮后 Hook

- [x] **FR1：新增 `agent/after-turn` Hook 契约**
  - 新增类型化 `AfterTurnPayload`，至少包含 Agent、Session、Turn、request、
    outcome/status、实际使用的 AgentConfig 和 cancellation；
  - Hook 为可等待的 waterfall/serial 控制边界，默认行为必须允许继续消费；
  - Hook 不进入 Mailbox 持久化，不替代 `agent/turn-stopping`；
  - Hook 失败必须能被 Lane 识别为“轮后维护失败”，而不是静默吞掉。

- [x] **FR2：SessionLane 在下一轮前触发 after-turn**
  - Turn 完整持久化、CompletionRegistry 完成并发布快照后，dispatch
    `agent/after-turn`；
  - Hook 完成前不得领取下一条 pending；
  - 队列为空时仍然触发，保留当前轮后预压缩语义。

### 3.2 ftre-compaction 接管主动门控

- [x] **FR3：pre-step Hook 接管领取前强制压缩**
  - `ftre-compaction` 在 `agent/pre-step` 中读取当前 Agent 配置；
  - 继续使用 `compact_threshold`（默认 0.8）和当前 pending 消息的额外 token 估算；
  - 超过强制线时，Hook 等待 `CompactionService` 完成，再重新检查水位；
  - 压缩未达到安全水位或执行失败时，返回 `RejectStep("keep", reason)`，队首保留；
  - 该路径不得 fail-open；日志和公开状态必须包含结构化失败原因。

- [x] **FR4：after-turn Hook 接管轮后预压缩**
  - 使用 `precompact_threshold`（默认 0.7）；
  - 使用本轮解析得到的 AgentConfig，不重新读取全局 default 配置；
  - 压缩期间阻塞下一条 pending，压缩成功后允许 Lane 继续；
  - 失败时进入可恢复的 blocked 状态，不删除已经完成的 Turn。

- [x] **FR5：保留 request-error overflow recovery**
  - `agent/request-error` 继续识别 `overflow`、`context_length`、`too_long`；
  - 只在压缩 progress generation 前进后返回 `RetryRequest`；
  - 每次请求最多因压缩重试一次，无进展不得循环重试。

### 3.3 移除 Lane 的压缩策略依赖

- [x] **FR6：SessionLane 不再直接调用 CompactionService**
  - 删除 `lane.py` 对 `ContextGate` 的压缩判断和 `_compact_or_block()` 的直接调用；
  - `lane.py` 不再导入 `CompactionService` 或读取 `compact_threshold`；
  - Lane 只调用类型化 Hook Runtime，并依据 Hook 结果决定 claim/继续/blocked。

- [x] **FR7：删除压缩专用 ContextGate Owner**
  - 删除或改造 `runtime/loop/context_gate.py`，不得继续作为压缩策略 Owner；
  - 不新增同义的 `CompactionGate`、`CompactionPort` 或 facade；
  - 如果为保持公开状态需要保留维护阶段状态，必须使用已有 Lane 状态模型或在评审
    中冻结一个最小通用维护状态，不重新引入压缩依赖。

- [x] **FR8：状态、取消和生命周期桥接**
  - 自动压缩仍能向客户端投影 `compacting`、完成和失败事件；
  - `blocked` 原因可观察，且不会把已完成 Turn 标记为失败；
  - Lane close、Plugin unload、Gateway stop 对 in-flight Hook 和压缩 Task 有明确
    等待/取消顺序；
  - 普通 `/cancel` 不得误取消共享压缩 Task，Gateway 关闭可以取消真实 Task。

### 3.4 清理与文档

- [x] **FR9：清理重复门控代码**
  - 删除 `ContextGate.before_claim/after_turn/compact` 中的压缩策略实现；
  - 删除 Lane 中只服务于旧 ContextGate 的字段、方法和注释；
  - 保留必要的通用 Lane 状态和生命周期代码，不保留单行兼容壳。

- [x] **FR10：架构门禁与文档同步**
  - 新增门禁：`SessionLane` 不得 import `CompactionService`、`ContextGate`；
  - 新增门禁：主动压缩 Hook 只存在于可选 `ftre-compaction` 包，ftre 核心不得保留第二套实现；
  - 更新 F6/F10 相关描述、TODO、CHANGELOG 和执行报告；
  - 在 PRD 变更记录中记录状态桥接和 Hook 失败语义的最终决策。

### 3.5 可选包与独立发行物

- [x] **FR11：压缩包与核心包物理解耦**
  - 将压缩实现、压缩 Hook、压缩命令和压缩配置收敛到独立发行物
    `ftre-compaction`（最终 Python distribution 名称在 approved 阶段冻结）；
  - 独立包只能依赖 ftre 已发布的公开 Hook、Session、LLM/配置 Service 契约，
    不得 import `SessionLane`、`TurnExecutor`、`AgentLoop` 私有模块；
  - 独立包入口使用标准 `module:apply` Plugin 入口，并通过 Manifest/entry point
    显式启用；
  - `CompactionService`、Hook 注册、`/compact` 和 `/compress-fast` 属于同一个
    可选包的 Owner，安装者不需要理解多个压缩包的组合细节。

- [x] **FR12：未安装/未启用时零耦合运行**
  - ftre 核心启动路径不得 import 压缩发行物或要求 `compaction` Service；
  - 未安装或未启用压缩包时，`agent/pre-step` 和 `agent/after-turn` 使用默认继续行为，
    SessionLane、AgentLoop、Command、Gateway 正常启动；
  - 未启用时不注册压缩命令；即使客户端显式请求，也只能得到稳定的“命令不可用”
    结果，不得产生 ImportError、Service 缺失异常或 Gateway 500；
  - 安装并启用压缩包后，所有压缩行为只由该包提供，核心无需增加条件分支。

- [x] **FR13：独立安装和发行验证**
  - 在不依赖 ftre sibling checkout 的临时虚拟环境中构建并安装该包；
  - 仅安装 `ftre` 时 Gateway smoke 通过，安装 `ftre + ftre-compaction` 后压缩专项通过；
  - 包元数据声明明确的 ftre 兼容版本，不能依赖未发布的本地路径；
  - 记录 wheel/sdist、import origin、Plugin discovery 和卸载后的启动证据。

## 4. 技术方案

### 4.1 目标调用顺序

```text
SessionLane._drain()
  1. mailbox.peek(session_id)
  2. resolve_inbound_config(item)
  3. dispatch(agent/pre-step, payload)
       └─ compaction plugin: should_compact → compact → recheck
  4. EnterStep 成功后 mailbox.take(session_id, request_id)
  5. TurnExecutor.execute(...)
  6. persist completion + publish snapshot
  7. dispatch(agent/after-turn, payload)
       └─ compaction plugin: compact_if_needed(threshold=0.7)
  8. 回到第 1 步
```

Hook 失败时，Lane 只处理统一结果：

```text
pre-step RejectStep(keep) → 保留队首 → Blocked
after-turn failure       → 不领取下一条 → Blocked
request-error RetryRequest → 当前 Turn 有界重试
```

### 4.2 Hook 契约草案

```python
@dataclass(frozen=True, slots=True)
class AfterTurnPayload:
    agent: AgentSubject
    session_id: str
    turn_id: str
    request_id: str
    status: str
    config: AgentConfig
    cancellation: asyncio.Event


AGENT_AFTER_TURN_SPEC = HookSpec(
    "agent/after-turn",
    "agent",
    HookMode.WATERFALL,
    failure_policy=HookFailurePolicy.PROPAGATE,
    payload_type=AfterTurnPayload,
    result_type=type(None),
    default=_continue_after_turn,
    scope=HookScope.AGENT,
)
```

第一版不新增 `CompactionPort`、`WaitStep` 或 `AgentControlPort`。压缩 Hook 直接
等待 `CompactionService`，成功后调用 `next_()`；失败通过现有 Hook 失败策略交给
Lane 统一进入 blocked。若评审认为客户端必须在 Hook 等待期间获得独立的
`compacting` phase，再单独冻结最小状态桥接，不在实现中临时发明类型。

### 4.3 目标文件树

```text
src/ftre/
├─ services/agent/hooks.py
│  └─ 新增 agent/after-turn 契约
├─ platform/hooks/names.py
│  └─ 注册稳定 Hook 名称
├─ services/agent_loop/runtime/mailbox/lane.py
│  ├─ 保留 peek → Hook → claim → execute 串行状态机
│  ├─ 增加轮后 Hook barrier 和通用 maintenance 状态桥
│  └─ 移除直接压缩判断、ContextGate 和 CompactionService 依赖
├─ services/session/events.py
│  └─ 提供通用 SessionEventService 与维护事件名
└─ （不包含任何 compaction Service、Feature 或兼容入口）
```

F11 完成后的独立发行物目标树为：

```text
packages/ftre-compaction/
├─ pyproject.toml
├─ README.md
├─ src/ftre_compaction/
│  ├─ plugin.py              # 唯一 module:apply 入口
│  ├─ service.py             # CompactionService 与压缩并发/算法
│  ├─ hooks.py               # pre-step / after-turn / request-error
│  ├─ commands.py            # compact / compress-fast
│  ├─ config.py              # 压缩配置快照与摘要模型解析
│  ├─ events.py               # core SessionMaintenanceEvent 的包内别名
│  └─ __init__.py
└─ tests/
   ├─ test_hooks.py
   ├─ test_plugin.py
   ├─ test_config.py
   ├─ test_compact_algo.py
   └─ test_compact_summary.py
```

ftre 主包只保留稳定 Hook、Session、LLM 和 Plugin Runtime 契约，不保留压缩实现的
兼容 re-export。实现已经移动到 `packages/ftre-compaction`；主包没有兼容入口、No-op
fallback 或同名空目录。

### 4.4 独立包运行契约

独立包必须把“安装”和“启用”分成两个动作：安装只提供可发现的发行物，只有用户在
Plugin 配置中显式启用后才创建 Service 和注册 Hook。概念性入口如下：

```toml
[project]
name = "ftre-compaction"
dependencies = ["ftre>=<最低公开版本>"]

[project.entry-points."ftre.plugins"]
compaction = "ftre_compaction.plugin:apply"
```

```python
# ftre_compaction/plugin.py
inject = ("config", "sessions", "hook_runtime", "commands")
provide = ("compaction",)


def apply(ctx, config=None):
    service = CompactionService(
        session_manager=ctx.sessions,
        config_service=ctx.config,
    )
    ctx.provide("compaction", service)
    register_compaction_hooks(ctx, service)
    register_compaction_commands(ctx, service)
    ctx.effect(lambda: service.close, label="compaction:close")
```

上面的代码只表示所有权关系，不冻结具体 Python 参数名。正式接口必须满足：

- `ftre-compaction` 只 import ftre 的公开模块，不 import `src/ftre/services/agent_loop`
  私有实现；
- Hook Receipt、命令注册、后台 Task 和 Service close 全部绑定 Plugin Fiber；
- `ftre` 未安装该包时，Composition 不解析此 entry point；
- `ftre-compaction` 未启用时，不创建 `compaction` Service，也不注册任何压缩命令；
- `ftre` 核心不通过 `try/except ImportError` 隐式加载或降级到 No-op 实现。

### 4.5 失败与生命周期

| 场景 | 期望行为 |
|---|---|
| pre-step 压缩成功 | 返回 `EnterStep`，随后才 claim |
| pre-step 压缩失败 | 返回 keep/block，队首仍在 pending |
| after-turn 压缩失败 | 已完成 Turn 保留，下一条不 claim，Lane 可恢复 |
| 未安装压缩包 | Hook 默认继续，Gateway 正常启动，压缩命令不注册 |
| 压缩包卸载 | 新 Hook 不再进入，已执行 Hook/Task 等待收敛后释放 |
| Hook 被卸载 | 不再接收新调用，等待 in-flight 调用收敛 |
| Gateway stop | 取消/等待真实压缩 Task，不能写入已关闭 Session |
| overflow 无进展 | 不返回 RetryRequest，保留原始错误 |
| 重复消息/重放 | 仍由 Mailbox admission 幂等处理，不由 Hook 去重 |

## 5. 验收标准

- [x] **AC1：Hook 契约**
  - `agent/after-turn` 有类型化 payload、默认行为、失败策略、scope 和诊断；
  - `agent/turn-stopping` 的 Core 语义不受影响。

- [x] **AC2：领取前门控**
  - 压缩 Hook 运行时队首仍在 pending；
  - 压缩成功后只 claim 一次；失败时 pending 不丢失、不重复消费。

- [x] **AC3：轮后门控**
  - Turn 完成后即使 mailbox 为空也执行 after-turn；
  - after-turn 未完成前下一条 pending 不会被领取。

- [x] **AC4：阈值与配置**
  - 80% 使用 `compact_threshold`；70% 使用 `precompact_threshold`；
  - 使用本轮实际 Agent 的 `context_window`、`max_output` 和 `safety_buffer`；
  - token 水位算法仍由 `CompactionService.should_compact()` 唯一实现。

- [x] **AC5：失败与恢复**
  - 压缩失败进入可观察 blocked，队首保留；取消 pending 后 Lane 可继续；
  - overflow 仅在 progress generation 前进后重试一次；
  - Hook 失败不会让 SessionLane worker 静默退出并遗失 pending。

- [x] **AC6：生命周期**
  - Plugin unload/restart 等待 in-flight after-turn/pre-step；
  - Gateway stop 和 Session close 不留下压缩 Task、监听器或旧 Service 引用；
  - 普通取消、Gateway 取消、Hook unload 三种语义有独立测试。

- [x] **AC7：架构边界**
  - `lane.py` 不再引用 `CompactionService`、`ContextGate` 或压缩阈值字段；
  - 主动压缩策略只位于可选 `ftre-compaction` 包；
  - 不新增 Port、Facade、AgentControlPort 或第二个 Compaction Owner。

- [x] **AC8：行为回归**
  - 自动压缩、手动 `/compact`、`/compress-fast`、overflow recovery、事件投影、
    mailbox pending 和客户端状态协议保持现有行为。

- [x] **AC9：质量门禁**
  - `python -m pytest -q tests/contracts tests/architecture tests/lifecycle packages/ftre-compaction/tests`
    通过；
  - `python -m pytest -q` 通过；
  - `python -m ruff check --no-cache src tests packages/ftre-compaction/src packages/ftre-compaction/tests` 通过；
  - `git diff --check`、YAML 校验和 Gateway 启停 smoke 通过。

- [x] **AC10：可选安装**
  - 在未安装/未启用压缩包的环境中，Gateway、普通消息、Tool、Session 和 Command
    基线均通过；不存在 `compaction` 缺失导致的启动异常；
  - 未启用时请求 `/compact` 返回稳定的不可用结果，不进入 Agent Turn。

- [x] **AC11：独立发行物**
  - `ftre-compaction` 可在干净虚拟环境中独立构建 wheel/sdist 并安装；
  - 安装并显式启用后，`agent/pre-step`、`agent/after-turn`、`agent/request-error`
    三条 Hook 均由该包注册，自动/手动/快速压缩行为通过；
  - 卸载该包后核心仍可启动，Hook Runtime 无残留监听器、Task 和旧 Service 引用。

## 6. 测试计划

### 6.1 Hook 契约测试

- `agent/after-turn` 默认继续、顺序调用、失败传播、scope 隔离、重复 `next_()`；
- Hook unload/restart 与 in-flight drain；
- `ftre-compaction` 注册 `pre-step`、`after-turn`、`request-error` 三条监听。

### 6.2 SessionLane 状态机测试

- pre-step 压缩时队首仍在 pending；
- pre-step 压缩失败、after-turn 压缩失败均进入 blocked；
- after-turn 完成前不领取下一条；
- 队列为空时轮后预压缩仍执行；
- 取消 pending、关闭 Session、Gateway stop 不丢消息；
- 同 Session 不发生 Turn/Compaction 并发，多个等待者共享同一压缩 Task。

### 6.3 回归测试

- 手动 `/compact` 和 `/compress-fast`；
- overflow recovery 有进展重试和无进展不重试；
- compact start/done/failed 事件和客户端状态快照；
- 多 Agent 使用各自 context window；
- 命令绕过 Agent Turn，普通用户消息仍经过 pending。

### 6.4 可选包与洁净环境测试

- 仅安装 ftre：无压缩 Service 时 Gateway 启动、普通消息和 after-turn 默认继续；
- 安装并启用 ftre-compaction：验证三个压缩 Hook、命令注册、Service 生命周期；
- 禁用/卸载 ftre-compaction：验证核心无导入错误、无缺失依赖和无残留监听器；
- 构建 wheel/sdist，检查元数据、依赖和 `module:apply` 入口；
- 在没有 `E:\ftre` sibling source 的临时环境中执行最小 Gateway smoke。

## 7. 开放评审项

本阶段已冻结以下实现决策：

1. 客户端继续收到 `compacting`；Lane 内部使用不含压缩算法的
   `MaintenanceOperation`，Hook 通过 `set_maintenance(active, reason)` 更新状态；
2. pre-step 压缩失败返回 `RejectStep("keep", reason)`，after-turn/Hook 异常由
   `BlockedOperation` 统一承载，已完成 Turn 不回滚；
3. Session close 先设置共享 cancellation signal 并取消 worker，ftre-compaction
   捕获取消后调用自身 `CompactionService.cancel_compact(session_id)`，Plugin close
   再取消剩余共享 Task；
4. pre-step 与 after-turn 均使用 `HookFailurePolicy.PROPAGATE`；观察型 Hook 使用
   独立 EMIT Spec，不改变控制型 Hook 的安全门。

`agent/after-turn` 名称和“压缩必须完全由可选包提供”的目标已在本轮需求中冻结，
不再作为开放项。

## 8. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-22 | 初始草案：新增 `agent/after-turn`，将主动压缩门控迁移到 Compaction Feature Hook，保留 Lane 的队列串行、pending 安全、状态和生命周期责任 | 当前压缩实现虽已拥有 Service Owner，但主动压缩策略仍分散在 SessionLane、ContextGate 和 Feature Hook；需要在不破坏 pending/并发/客户端协议的前提下完成 Hook 化 |
| 2026-08-22 | 根据评审目标补充：压缩 Hook、Service、算法、命令和配置必须收敛为可独立安装/发布的 `ftre-compaction` 包；ftre 核心不要求 compaction Service，未安装时 Hook 默认继续、Gateway 正常启动 | 压缩应成为真正可选的产品能力，启用、调整或替换压缩时只需要维护一个包，不应让 Agent 核心承担压缩依赖 |
| 2026-08-22 | 实施完成：新增 `agent/after-turn`，SessionLane 改为 Hook barrier，删除 ContextGate 与核心压缩依赖；CompactionService、三条 Hook、压缩命令和算法迁入 `packages/ftre-compaction`，核心默认组合不再启用压缩 | 让压缩成为可安装、可启用、可卸载的独立能力，同时保持 pending、串行、状态和客户端协议不变 |
| 2026-08-22 | 配置 Owner 收尾：删除核心 AgentConfig 中的压缩字段，新增 `ftre_compaction.config.CompactionConfig`；Hook/Command 从 ConfigService 快照读取压缩设置，并清理无消费者的历史配置示例 | 消除“行为已在可选包、配置却仍在核心”的重复 Owner，使压缩包可以独立调整阈值和摘要模型 |
| 2026-08-24 | 修复 AgentLoop 构造 `agent/after-run` Payload 时遗漏本轮配置和维护状态回调的问题；压缩 Hook 现在可真正执行轮后预压缩，并在压缩期间让 Session 保持 `compacting/busy`，避免 Inbox 提前领取下一条消息 | 原实现会因 `payload.config` 为空直接跳过轮后压缩，且无法接通 `set_maintenance`；补充回归测试，F11 的 AC8/AC9 重新核验通过（全量 513 passed） |
