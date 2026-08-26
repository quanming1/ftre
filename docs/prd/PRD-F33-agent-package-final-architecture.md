# PRD-F33 Agent Package 终局架构

> 这份 PRD 固定 ftre 的最终理想形态。它记录目标和边界，不代表当前代码已经完成，
> 也不授权跳过 F31、F32 直接搬迁实现。F34 ToolService 终局契约见
> `docs/prd/PRD-F34-tool-service-runtime.md`。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F33 |
| 名称 | Agent Package 终局架构 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-26 |
| 二次审查日期 | 2026-08-26 |
| 定稿日期 | 2026-08-26 |
| 验收日期 | 2026-08-26 |
| 关联文档 | `docs/TODO.yaml` F33；`docs/prd/PRD-F30-llm-service-package.md`；`docs/prd/PRD-F31-agent-service-boundaries.md`；`docs/prd/PRD-F32-agent-runtime-service-decoupling.md`；`docs/prd/PRD-F34-tool-service-runtime.md`；`AGENTS.md` |

---

## 1. 终局目标

### 1.1 一句话目标

将 Agent 的稳定契约与具体执行实现拆成两个独立 Package，使 ftre Host 只负责组合
Service 和 Plugin，Agent Runtime 只通过公开 Service/Hook 契约工作，最终不存在第二个
Agent Owner、隐藏的跨层实现依赖或临时桥接入口。

### 1.1.1 用户意图的具体化

F33 不是为了“目录看起来像 DSH”，而是为了让以后修改 Agent 时只需要关注 Agent Package，
不必同时理解 Queue、Session 持久化、Command、Tool、LLM Provider、Compaction 或客户端。
最终用户可获得以下结果：

1. 想替换 Agent 执行算法，只替换/升级 `ftre-agent-runtime`，不修改 Inbox、Channel 和客户端；
2. 想增加 Agent 行为，只安装或卸载监听 Agent/LLM/Tool Hook 的 Plugin，不修改 AgentLoop；
3. 想复用 Agent 契约，直接安装 `ftre-agent`，不安装 Gateway、Session 数据库或客户端；
4. 想禁用压缩、Retry 或 Fallback，只调整对应 Package，Agent 基础执行仍可运行；
5. 任意 Session 的普通消息、Steering、Tool、LLM 输出和刷新恢复，仍保持当前用户可见行为。

### 1.2 终局分层

```text
Kernel       = 与业务无关的运行机制
Service      = Host 提供的稳定运行时能力
Plugin       = 能力的装配、生命周期和可选行为
Package      = 安装、版本和发布边界
Agent        = 稳定的 Agent Service 契约
Runtime      = AgentLoop/TurnExecutor 的具体执行实现
Core         = ReAct、Tool、LLM 算法引擎
```

必须始终满足：

1. Kernel 不认识 Agent、Session、Queue、Tool、LLM、Compaction、Command 或 Channel。
2. Agent Service 不拥有 AgentLoop、队列、Command、Channel、Session Repository 或压缩。
3. Agent Runtime 不直接依赖 Host Service 的 Provider、Repository、Registry 或私有属性。
4. Service 之间只能通过 `Inject` 使用稳定公开 Service；禁止 Service Locator、全局 setter、
   `bind_*`、隐式业务查找和跨 Owner 私有 import。
5. 每个能力只有一个创建、注册、销毁和发布 Owner。
6. 可选行为通过 Plugin + Hook 加入；卸载 Plugin 后其 Hook、Route、Task、Listener 和
   资源必须全部消失。
7. 不为单一实现增加 Port、Facade、Coordinator、转换层或兼容壳。
8. Agent Hook 和 LLM StreamChunk 各自只有一套稳定协议。

### 1.3 DSH 复核后的取舍

本节是 F33 的设计依据。已重新阅读 `E:\deepseek-harness` 的架构文档和以下真实实现：

- `packages/core/agent/src/index.ts`：`ctx.agents`、Agent 注册、Agent Factory、作用域和生命周期；
- `packages/core/agent/src/inbox.ts`：Agent 内置 `next-turn` / `next-step` Inbox；
- `packages/core/agent-loop/src/index.ts`、`src/agent.ts`：具体 Loop Provider、注入依赖、Turn/Step 驱动；
- `packages/core/session/src/index.ts`：事件溯源 Session、`session/event`、`session/flush`；
- `packages/core/tools/src/index.ts`：Tool Definition、执行管线和 Tool Hook；
- `packages/interaction/commands/src/index.ts`：命令直达执行，不进入模型 Turn；
- `docs/architecture.md`：Plugin、Service、事件和 Turn 流程总原则。

我们采用 DSH 的结构性经验，但不复制它的全部模型：

| DSH 做法 | ftre 的采用方式 | 不采用的部分 |
|---|---|---|
| `ctx.agents` 是稳定 Agent Service，具体 Loop 另由 Provider Plugin 提供 | `agents` 是唯一 Host Agent Service key；Runtime Package 作为 Provider Plugin 装载 | 不新增公开 `agent_runtime` key，不让 Host 同时维护两个 Agent Service |
| Agent Loop 只注入 Session、Prompt、Tools、LLM 等能力 | Runtime 只通过显式 Inject 消费 Host Service | 不把 Provider、Repository、Manager 或 Channel 具体实现传入 Runtime |
| Agent 事件区分 durable Session event 与 live Hook | 保留 Session 持久化和 Agent/LLM/Tool Hook 的分工 | 不在 F33 重新命名或复制 Core 已拥有的 Hook |
| Agent 自带 `next-turn` / `next-step` Inbox | ftre 由 `ftre-inbox` Package 完整拥有队列、pending、claim、steer 和 worker | 不把 Inbox、QueueItem、next-turn、next-step 放进 `ftre-agent` 或 Runtime |
| Agent Factory 支持多消费者创建/恢复 Agent | 只有在 ftre 已有真实消费者需要时才提供创建 API | 不为了对齐 DSH 凭空加入 `AgentHandle`、Factory、Coordinator 或新的 DTO |
| 所有能力通过 Cordis Plugin/Effect 可卸载 | Runtime、Hook、Task、Listener 均绑定 Runtime Plugin Fiber | 不把 Bootstrap 手工构造伪装成 Plugin 生命周期 |

F33 的核心结论是：**借鉴 DSH 的“稳定 Agent Service + 可替换 Loop Provider”，保留
ftre 的“队列完全独立 Package + Agent 只消费 InboundMessage”决策。**

### 1.4 过渡代码纪律

```text
当前：src/ftre/services/agent/runtime/
      + 临时 Host/Core 接线

终局：packages/ftre-agent/
      packages/ftre-agent-runtime/
      src/ftre/ 只保留 Host、Service、Plugin、Kernel
```

迁移桥只允许短期存在，不能成为公开 API。F33 收尾时必须删除旧入口、旧目录、重复
Owner、`core_bridge` 和兼容 alias。

---

## 2. 终局文件树

```text
E:/ftre/
├─ pyproject.toml
├─ README.md
├─ AGENTS.md
│
├─ packages/
│  ├─ ftre-agent/                         # 稳定 Agent Service 契约
│  │  ├─ pyproject.toml
│  │  ├─ README.md
│  │  ├─ README.zh.md
│  │  └─ src/ftre_agent/
│  │     ├─ __init__.py
│  │     ├─ service.py                    # AgentService 公开运行入口
│  │     ├─ contracts.py                  # InboundMessage / AgentRunResult / AgentStatus
│  │     ├─ registry.py                   # AgentInfo / Registration
│  │     ├─ status.py                     # AgentStatus
│  │     └─ hooks.py                      # Agent HookSpec 与 Payload
│  │
│  ├─ ftre-agent-runtime/                 # AgentLoop 的具体实现
│  │  ├─ pyproject.toml
│  │  ├─ README.md
│  │  ├─ README.zh.md
│  │  └─ src/ftre_agent_runtime/
│  │     ├─ __init__.py
│  │     ├─ plugin.py                     # Provider Plugin
│  │     ├─ engine.py                     # AgentLoop
│  │     ├─ turn_executor.py              # Turn 状态机
│  │     ├─ factory.py                    # 唯一 Core Agent 创建点
│  │     ├─ state.py                      # Run / Turn 私有状态
│  │     └─ completion.py                 # 进程内完成状态
│  │
│  ├─ ftre-llm/
│  ├─ ftre-inbox/
│  ├─ ftre-compaction/
│  ├─ ftre-llm-recovery/
│  ├─ ftre-llm-fallback/
│  └─ …其他独立业务 Package
│
├─ src/ftre/
│  ├─ app/
│  │  └─ gateway/
│  │     ├─ composition.py                # 唯一 Composition Root
│  │     ├─ bootstrap.py                  # 进程启动/关闭
│  │     └─ http/                         # Gateway Host 适配
│  │
│  ├─ kernel/
│  │  ├─ hooks/                           # Hook 注册、作用域、分发、清理
│  │  └─ plugins/                          # Manifest、Discovery、Loader、Manager
│  │
│  ├─ services/                           # Host 稳定 Service
│  │  ├─ config/
│  │  ├─ filesystem/
│  │  ├─ http/
│  │  ├─ sessions/
│  │  ├─ messaging/
│  │  │  ├─ bus/
│  │  │  └─ channel/
│  │  ├─ tools/                           # ToolService，详见 F34
│  │  ├─ system_prompt/
│  │  ├─ agent_profile/
│  │  ├─ llm/
│  │  ├─ attachments/
│  │  └─ workspace/
│  │
│  └─ plugins/
│     └─ builtin/                          # 产品行为与 concrete adapter
│        ├─ command/
│        ├─ channels/
│        │  ├─ websocket/
│        │  └─ subagent/
│        ├─ trace/
│        ├─ skill/
│        ├─ mcp/
│        └─ schedule/
│
└─ tests/
   ├─ architecture/
   ├─ contracts/
   ├─ lifecycle/
   ├─ startup/
   └─ integration/
```

### 2.1 目录 Owner

| 目录 | 唯一职责 | 明确不拥有 |
|---|---|---|
| `kernel/` | Context、Inject、Hook、Fiber、Plugin 生命周期 | 产品业务状态 |
| `services/` | Host 稳定能力及 Provider Plugin | Agent Runtime 具体实现 |
| `plugins/builtin/` | Command、MCP、Skill、Schedule 等行为 | AgentLoop 内部状态 |
| `ftre-agent/` | Agent 稳定契约、注册和状态模型 | ReAct 算法、Session、Queue |
| `ftre-agent-runtime/` | AgentLoop、TurnExecutor、Core 接线 | Host Service Provider |
| `ftre-agent-core` | ReAct、Tool、LLM 算法 | Gateway、Plugin、Session 持久化 |

### 2.2 Ftre 与 DSH 的边界差异

DSH 的 `Agent` 对象公开 `inbox.nextTurn`、`inbox.nextStep`、`followup()`、`steer()` 和
`inject()`，因为它把消息队列作为 Agent Package 的内部组成部分。ftre 明确不采用这一点：

```text
ftre-inbox Package
  ├─ Inbound admission、pending、容量、claim、worker、steer
  └─ 只把已交付 InboundMessage 传给
       ↓
ftre-agent AgentService
  └─ 只负责 active Run、取消、状态和结果
       ↓
ftre-agent-runtime
  └─ 只负责 Turn / Step 执行
```

因此，任何 `Agent` 公共 API 或 Runtime 私有状态出现以下字段都视为 F33 失败：

```text
QueueItem / pending / claim / next-turn / next-step / mailbox / queue worker
```

---

## 3. 终局依赖图

```text
                         ┌──────────────────────────┐
                         │       ftre Host           │
                         │ Composition / Bootstrap  │
                         └────────────┬─────────────┘
                                      │ loads
             ┌────────────────────────┼────────────────────────┐
             │                        │                        │
       Host Services            Builtin Plugins          Optional Packages
   sessions / tools / llm     command / channel / mcp    inbox / compaction /
   prompt / profiles / bus   skill / schedule / trace   retry / fallback
             │                        │                        │
             └─────────────── Inject / Hook ───────────────────┘
                                      │
                         ┌────────────▼─────────────┐
                         │   ftre-agent-runtime      │
                         │ AgentLoop / TurnExecutor  │
                         └────────────┬─────────────┘
                                      │ implements / publishes
                         ┌────────────▼─────────────┐
                         │       ftre-agent          │
                         │ AgentService / contracts │
                         └──────────────────────────┘
```

```text
ftre-agent-runtime  ───────────── calls ────────────►  ftre-agent-core
```

Runtime 对 Core 的调用是单向的：`Runtime → ftre-agent-core`；Core 不认识 `ftre-agent`
或任何 ftre Host Service。`ftre-agent` 只是 Runtime 对外稳定契约，不是算法调用方。

允许的方向：

```text
Host Service  →  Runtime Plugin  →  ftre-agent contracts
                     │                    │
                     └──────────────→ ftre-agent-core
```

禁止出现：

```text
Runtime → SessionRepository
Runtime → ChannelManager
Runtime → ToolRegistry 私有实现
Runtime → AgentProfileManager
Runtime → Provider 工厂
Host Service → Runtime 私有模块
```

---

## 4. Agent Service 契约

`AgentService` 是稳定运行入口，不是 AgentLoop 的别名，也不保存具体 Turn 状态。
它只负责 Agent 身份/作用域、active Run 状态、取消和把一条已交付输入交给 Runtime。
队列、命令、压缩和 Channel 都不进入该契约。

```python
class AgentService:
    key = "agents"

    async def run(self, message: InboundMessage) -> AgentRunResult: ...
    async def cancel(self, session_id: str, *, expected_request_id: str = "") -> bool: ...
    def status(self, session_id: str) -> AgentStatus: ...
    def is_busy(self, session_id: str) -> bool: ...
    async def delete_session(self, session_id: str) -> None: ...
    async def resume_confirmation(...) -> AgentRunResult: ...
```

这是 F32 已有调用面的包化版本。本 PRD 固定 `AgentRunResult` 为唯一公开结果名称；F33.2
将当前 `TurnOutcome` 的字段迁移到该类型并一次性切换调用方，不保留兼容别名。不得同时保留 `AgentDriver`、`AgentRuntime`、`TurnOutcome`、`AgentResult` 四套
同义结果模型。`AgentDriver + AgentLoopDriver + attach_driver()` 是当前过渡接线，
终局最多保留一个 Runtime 调用契约；若没有跨 Host 的第二个实现，优先改为 Runtime
Provider 内部闭包/构造注入，而不是继续公开一个 Port。

```python
AgentStatus = Literal["idle", "running", "processing", "compacting"]


@dataclass(frozen=True, slots=True)
class InboundMessage:
    session_id: str
    request_id: str
    channel_id: str
    content: str = ""
    attachments: tuple[dict[str, Any], ...] = ()
    source: str = "user"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    session_id: str
    turn_id: str
    status: Literal["completed", "cancelled", "failed"]
    user_message_id: str = ""
    final_content: str = ""
    error: Mapping[str, Any] | None = None
```

约束：

- `InboundMessage` 是唯一执行输入，不包含 QueueItem、Command 或 Channel 私有类型；
- `AgentRunResult` 是当前 `TurnOutcome` 的稳定包化结果，不直接暴露 Session Repository
  或 Core Runner；
- 取消、失败和正常结束必须是可区分的稳定语义；
- 集合和配置在进入 Runtime 前冻结；
- API Key、完整凭据和 Provider 私有对象不得进入请求或结果。

### 4.1 Agent Hook 的真实 Owner

```text
Ftre Agent Package 定义并发布：
  agent/before-run
  agent/after-run
  agent/run-error

ftre-agent-core 定义并发布（ftre 只接入，不复制）：
  agent/before-reasoning
  agent/stop-decision

ftre-llm / Core 定义并发布：
  agent/request
  llm/stream
  llm/error
```

F33 不新增 `agent/run-start`、`agent/step-completed`、`agent/run-end` 同义 Hook；如果
需要这些语义，必须先证明现有 Hook 无法表达，并另行修改 PRD/Owner。Runtime 只负责
发布 Ftre Agent Hook；Core Hook 的 Spec、Payload、Result 仍由 `ftre-agent-core` 唯一拥有。

| Hook | 定义 Owner | 发布者 | 典型监听者 | 失败语义 |
|---|---|---|---|---|
| `agent/before-run` | `ftre-agent` | Runtime | 权限、治理 | 可拒绝本次 Run |
| `agent/after-run` | `ftre-agent` | Runtime | Compaction、Trace | 已提交结果后维护，不能改写结果 |
| `agent/run-error` | `ftre-agent` | Runtime | 恢复、诊断 | 只能返回稳定恢复决策 |
| `agent/before-reasoning` | Core | Core | Inbox Steer | 只在下一次 Core Reasoning 前注入 |
| `agent/stop-decision` | Core | Core | Steer、Continuation | 不得复制为 Ftre Hook |
| `agent/request` | LLM/Core | LLM/Core | 模型路由 | Waterfall，返回完整请求配置 |
| `llm/stream` | LLM/Core | LLM/Core | Fallback/审计 | 流包装，不负责业务重试 |
| `llm/error` | LLM/Core | LLM/Core | Retry/Fallback | 返回恢复决策，取消不触发 Fallback |

监听者不得访问 Runtime 私有状态，只能消费冻结 Payload。

---

## 5. Agent Runtime 契约

### 5.1 Runtime 只负责执行

```text
InboundMessage
    ↓
AgentService.run(InboundMessage)
    ↓
AgentLoop
    ↓
Reasoning → Tool → LLM
    ↓
Assistant Event / Session commit
    ↓
AgentRunResult
```

Runtime 不负责：

- pending、claim、队列排序和容量；
- Command 解析和 Command Result；
- Session 文件、Projection 或数据库写入；
- Compaction 调度和摘要算法；
- Channel 协议、WebSocket payload 和客户端状态；
- Provider Adapter 创建、Retry 次数和 Fallback 选择。

### 5.2 Runtime Plugin 接线

```python
inject = (
    "config",
    "sessions",
    "message_bus",
    "tools",
    "workspaces",
    "system_prompt",
    "agent_profiles",
    "llm",
    "hook_runtime",
    "session_events",
    # attachments / traces are optional and are passed explicitly as None when absent
)
provide = ("agents",)
```

Runtime Plugin 通过 Inject 构造运行时，并发布唯一的 `agents` Service。`config`、
`workspaces` 是 F32 已验证的必需依赖；`attachments`、`traces` 只能由 Provider 显式解析
后传入，Runtime 不调用 `ctx.get()`。Host Composition 只加载其 `module:apply` 入口，
不直接 `new AgentLoop`。

### 5.3 Core 边界

`ftre-agent-runtime` 可以依赖 `ftre-agent-core` 的公开 Agent、Tool、LLM 契约，但不得：

- 修改 Core 源码或复制 Core 实现；
- 让 Core 反向 import ftre Host；
- 在 Host 中维护第二个 ReAct/LLM 状态机；
- 把 `core_bridge.py`、`CoreLlmAdapter` 等临时转换层作为终局 API。

若 Core API 阻塞抽取，必须另开跨仓阶段，不在 F33 中偷偷修改。

### 5.4 Runtime 独立安装的依赖规则

`ftre-agent-runtime` 是可安装 Package，但它不是另一个 Host。为了满足 AC16，Runtime
源码不得直接 import `ftre.services.*` 的实现模块，否则会出现“Host 依赖 Runtime、Runtime
反向依赖 Host”的循环发行依赖。

本 PRD 固定采用**能力参数化**：Runtime 只依赖 `ftre-agent` 的公共输入/结果/Hook 类型，
Host Service 以构造参数传入；Runtime 代码不 import Host Service 类，只按已有公开方法调用。
如果某个 Core 类型确实无法脱离 Host，必须在 F33.1 记录一个最小、稳定、可复用的契约后再
提取；这不是默认方案，也不能借此把 Host 实现搬入 Runtime。

禁止方案：Runtime 依赖 `ftre` Host 包，同时 Host 根包再依赖 Runtime；禁止用
`core_bridge`、`AgentDriver`、`AgentLoopDriver` 或兼容 alias 掩盖循环。

F33.1 必须把当前实现中的反向 import 全部列入迁移结果，至少包括：

```text
ftre.services.agent.config
ftre.services.agent.contracts
ftre.services.agent.hooks
ftre.services.messaging.bus
ftre.services.session
ftre.services.session.message.multimodal
ftre.services.system_prompt.hooks
ftre.services.system_prompt.types
```

其中 `AgentConfig`、`PromptAssembly`、`BusMessage` 等 Host 数据结构不能原样搬进 Runtime
Package。要么由 Host 在调用前转换为已冻结的公共值，要么把确实跨包使用的最小字段提取到
`ftre-agent`；禁止以 `Any` 或隐式 `dict` 逃避边界，也禁止保留一个仅做字段转发的 Adapter。

---

## 6. Host Service 与 Plugin 边界

Host Service 只提供稳定、可注入的窄接口：

| Service | 提供能力 | Runtime 可消费的方向 |
|---|---|---|
| `sessions` | 消息读取、保存、更新、运行上下文 | 获取历史、保存结果 |
| `message_bus` | 业务消息发布 | 发布 Agent 事件 |
| `tools` | Tool 注册、作用域、Schema、统一执行和 Tool Hook | 构建 Schema、查询和执行 Tool |
| `system_prompt` | 结构化 Prompt 组装 | 获取最终 Prompt |
| `agent_profiles` | 解析不可变 Agent 配置 | 获取 Agent 配置 |
| `llm` | 模型解析、一次调用、流协议 | 发起 LLM 请求 |
| `hook_runtime` | Hook 注册与分发 | 发布 Agent 生命周期 |
| `session_events` | Session 事件出口 | 发出状态变化 |

ToolService 的详细终局契约以 F34 为准。Runtime 不得读取 `tools.registry`、直接调用
Tool 函数或构造第二个 Registry。

### 6.1 业务 Plugin

```text
Inbox Plugin       = admission、pending、claim、串行化
Compaction Plugin  = 压缩判断、摘要、压缩命令
Retry Plugin       = LLM 失败后的重试决策
Fallback Plugin    = 重试耗尽后的备用路由
Command Plugin     = 接入层命令解析与分发
Trace Plugin       = 观察和诊断
```

这些 Plugin 可以监听 Agent Hook 或消费公开 Service，但不能把行为塞回 AgentService 或
AgentLoop。

---

## 7. 终局运行流程

### 7.1 普通消息

```text
Channel Plugin
  ↓
InboundMessage
  ↓
Inbox Plugin（持久接纳、排队、claim）
  ↓
AgentService.run()
  ↓
ftre-agent-runtime
  ↓
agent/before-run
  ↓
Core agent/before-reasoning
  ↓
Core Reasoning / Tool / LLM
  ↓
Assistant Event / Session commit
  ↓
Session Service 持久化 + MessageBus/Channel 推送
  ↓
agent/after-run
```

### 7.2 Steering 消息

```text
用户发送消息
  ↓
InboundMessage(mode=queued)
  ↓
Inbox Plugin 持久化 pending
  ↓
用户点击 Steering
  ↓
Inbox Plugin 原子更新 placement=steering
  ↓
下一次 Core agent/before-reasoning 边界
  ↓
Runtime 消费 Steering InboundMessage
  ↓
Core 开始下一次 Reasoning
```

Steering 不需要 AgentService 认识队列；它由 Inbox/Steer Plugin 在 Agent Hook 边界完成。

### 7.3 压缩、Retry 和 Fallback

```text
Agent Hook
  ├─ Compaction Plugin：决定是否压缩并调用 ctx.llm
  ├─ Retry Plugin：监听 LLM 失败并返回 RetryRequest
  ├─ Fallback Plugin：在 Retry 耗尽后选择备用模型
  └─ Trace Plugin：观察，不改变业务结果
```

Agent Runtime 只发布边界并执行被接受的结果，不持有这些 Plugin 的实现。

### 7.4 DSH 风格的持久化/实时分工在 ftre 中的落地

DSH 的原则是“Model-visible means logged”：任何进入模型请求的消息都必须能从 Session
日志重建。ftre 采用同一原则，但队列由外部 Package 提供：

```text
ftre-inbox admission/claim
  └─ 生成已交付 InboundMessage
       ↓
Agent Runtime 在 Run/Step 边界将真实用户输入写入 Session Service
       ↓
Core 的 assistant/chunk、assistant/message、tool/result 按现有协议持久化
       ↓
Session Event / MessageBus / Channel 推送客户端
```

Steering 的新消息只有在 Core 下一次 Reasoning 边界真正进入模型时才成为模型可见内容；
它的用户语义、消息切分和 DB-first 持久化由 Inbox/Session/Runtime 现有协议共同保证，
F33 不改变 wire 格式。

---

## 8. Package 发行边界

### 8.1 `ftre-agent`

- 提供 AgentService、契约模型、注册模型、状态模型和 Agent Hook；
- 不依赖 Gateway、Session Repository、Inbox、Compaction 或客户端；
- 可以被测试替身和其他 Host 独立依赖；
- 不包含 AgentLoop、LLM Client 或 Tool 执行实现。
- 只允许依赖标准库、`cordis-py` 和已冻结的 Core-facing 类型；不得 import `ftre.services.*`。
- 它是契约包，不承担 Runtime Plugin，不注册 `agent-runtime` entry point。

### 8.2 `ftre-agent-runtime`

- 依赖 `ftre-agent`、`ftre-agent-core` 和 Host 稳定 Service 契约；
- 提供 Runtime Provider Plugin、AgentLoop、TurnExecutor 和 Driver；
- 不拥有 Host Service Provider、Queue、Command 或客户端协议；
- 通过 `ftre.plugins` entry point 暴露 `module:apply`；
- unload 时停止 Runtime、取消 in-flight Turn 并清理全部 Hook/Task。
- Host Service 依赖由 `apply(ctx)` 的 Inject 提供，不写成 Runtime 对 `ftre` 根包的安装依赖。

### 8.3 Host 默认组合

```text
Composition Root
  ├─ Host Service Providers
  ├─ ftre-agent-runtime Plugin
  ├─ ftre-inbox Plugin
  ├─ ftre-llm Provider Plugin
  └─ 其他可选 Builtin/External Plugin
```

Host 不得直接 import `ftre_agent_runtime.engine.AgentLoop` 手工组装，只通过 `agents`
Service 使用 Runtime。

### 8.4 发行元数据

| Package | `project.name` | `ftre.plugins` | 依赖原则 |
|---|---|---|---|
| Agent 契约 | `ftre-agent` | 无（契约包不是业务 Plugin） | 不依赖 Host；只依赖稳定契约所需的基础包 |
| Agent Runtime | `ftre-agent-runtime` | `agent-runtime = ftre_agent_runtime.plugin:apply` | 依赖 `ftre-agent`、`ftre-agent-core`、`cordis-py`；Host Service 由 Inject 提供 |

根 Host 的默认发行组合可以依赖两个 Package，但不能把两个 Package 的源码复制进
`src/ftre`，也不能因为默认安装就合并它们的 Owner。Wheel 需要分别验证 import、entry
point、卸载和缺失能力错误。

---

## 9. 具体改动清单与迁移映射

### 9.0 当前实现基线（F33 开工前）

截至 2026-08-26，F32/F34 已合入，但 F33 尚未实现：

- `src/ftre/services/agent/service.py` 仍是 `agents` Service 的实现；
- `src/ftre/services/agent/runtime/` 仍包含 `engine.py`（AgentLoop）、`turn_executor.py`、
  `factory.py`、`provider.py`、`driver.py` 和 `completion.py`；
- `src/ftre/services/agent/plugin.py` 仍通过 Host 路径创建 AgentService、AgentLoop 和 Driver；
- `src/ftre/app/gateway/composition.py` 的 `agents` Manifest 仍指向
  `ftre.services.agent.plugin:apply`；
- `packages/ftre-agent/` 和 `packages/ftre-agent-runtime/` 尚不存在；
- 现有测试直接 import `ftre.services.agent.runtime`，迁移后必须全部切换到 Package 公共入口
  或 Runtime 测试入口。

这些现状是待迁移事实，不得在执行报告中写成已完成的终局结构。

### 9.1 文件映射

| 当前路径 | F33 目标路径 | Owner | 处理规则 |
|---|---|---|---|
| `src/ftre/services/agent/service.py` | `packages/ftre-agent/src/ftre_agent/service.py` | ftre-agent | 迁移稳定 Service；删除 Host 内第二份实现 |
| `src/ftre/services/agent/contracts.py` | `packages/ftre-agent/src/ftre_agent/contracts.py` | ftre-agent | 迁移 `InboundMessage` 和唯一运行结果契约；不保留同义 alias |
| `src/ftre/services/agent/registry.py` | `packages/ftre-agent/src/ftre_agent/registry.py` | ftre-agent | 迁移 Agent identity/scope；不创建 Queue/Runtime Registry |
| `src/ftre/services/agent/hooks.py` | `packages/ftre-agent/src/ftre_agent/hooks.py` | ftre-agent | 仅迁移 Ftre-owned Run Hook；Core Hook 只引用，不复制 |
| `src/ftre/services/agent/runtime/engine.py` | `packages/ftre-agent-runtime/src/ftre_agent_runtime/engine.py` | Runtime | 迁移 active Run/维护状态；删除 Host 类型实现 import |
| `src/ftre/services/agent/runtime/turn_executor.py` | `packages/ftre-agent-runtime/src/ftre_agent_runtime/turn_executor.py` | Runtime | 迁移 Turn/Step；不得增加 Queue/Command/Compaction Owner |
| `src/ftre/services/agent/runtime/factory.py` | `packages/ftre-agent-runtime/src/ftre_agent_runtime/factory.py` | Runtime | 保留唯一 Core Agent 创建点 |
| `src/ftre/services/agent/runtime/driver.py` | Runtime 内部实现 | Runtime | 评估后删除 `AgentLoopDriver`；不得作为公共 Port 发布 |
| `src/ftre/services/agent/runtime/provider.py` | `packages/ftre-agent-runtime/src/ftre_agent_runtime/plugin.py` | Runtime Plugin | 通过 entry point `module:apply` 装载 |
| `src/ftre/services/agent/runtime/completion.py` | Runtime 私有模块 | Runtime | 仅保留进程内等待状态；不升级为 Service |
| `src/ftre/services/agent/config.py` | `src/ftre/services/agent/config.py` | Host Config Service | 不整体搬入 Agent Package；Runtime 通过稳定配置输入消费 |
| `src/ftre/services/agent/profile/` | `src/ftre/services/agent/profile/` | Agent Profile Service | 继续由 Host 提供，Runtime 不 import Manager |

### 9.2 实现顺序

1. **F33.1 基线**：用 AST/import/Manifest 生成当前 Owner 图；冻结真实消费者、Hook Owner、
   Runtime Inject 和包依赖策略。不得先移动文件再补解释。
2. **F33.2 契约包**：先创建 `ftre-agent` 的 `pyproject.toml`、README、稳定导出和契约测试；
   迁移后 ftre Host 与 Inbox 仍能通过同一个 `InboundMessage` 调用。
3. **F33.3 Runtime 包**：迁移 Engine、TurnExecutor、Factory、Completion 和 Provider；
   解决所有 `ftre.services.*` 实现 import，保持一次 Run 的行为不变。
4. **F33.4 Composition**：根 `pyproject.toml` 声明两个 Package；Manifest 改为 Runtime
   Package 的 entry point；Host 只通过 `ctx.agents` 使用 Agent Service。
5. **F33.5 删除旧 Owner**：删除 `src/ftre/services/agent/runtime/`、旧 Facade、
   `AgentDriver/AgentLoopDriver` 公共导出、兼容 alias 和测试旧 import；同步删除无消费者
   的监听器/辅助函数。
6. **F33.6 验收**：洁净安装两个 Package，验证 Composition、普通消息、Steering、Tool、
   Compaction、Retry、Fallback、取消、Session 删除、Plugin unload/restart 和并发。

### 9.3 迁移禁止项

- 不把 DSH 的 `Agent.inbox` 复制进 ftre；Inbox 仍由 `ftre-inbox` 唯一拥有。
- 不把 `config.py` 中的标题、压缩、Workspace 等 Host 配置整体塞进 `ftre-agent`。
- 不让 Runtime 通过 `ctx.get()`、`.registry`、`.manager`、`.projection` 或 Repository 反查能力。
- 不将 `agent/stop-decision`、`agent/before-reasoning` 在 ftre 再定义一份。
- 不为了满足“独立 Package”增加 `AgentRequest`、`AgentResult` 与 `TurnOutcome` 并存的转换层。
- 不修改 `E:\ftre-agent-core`、客户端、Inbox wire、Session wire 或 Cordis Kernel。

## 10. 迁移阶段

F33 依赖 F31、F32 和 F34 完成；不得跳过契约冻结直接移动目录。F33.1 必须先解决
Runtime 独立安装的 Host 依赖问题，并把选择写入执行报告，否则 F33.2/F33.3 不得开始。

| 批次 | 内容 | 结果 |
|---|---|---|
| F33.1 | DSH 复核、Owner/Hook/依赖基线、Runtime 独立安装策略 | 可执行迁移基线 |
| F33.2 | 抽取 `ftre-agent` 契约 Package，冻结唯一输入/结果模型 | Agent Service 可独立导入 |
| F33.3 | 抽取 `ftre-agent-runtime`，建立 Provider Plugin | Runtime 可由 Composition 装载 |
| F33.4 | Host 改为通过 `agents` Service 使用 Runtime | 删除 Host 手工 Agent 组装 |
| F33.5 | 删除旧 Agent Owner、Driver/Facade、桥接和兼容入口 | 只剩一个 Agent Owner |
| F33.6 | 洁净安装、生命周期、并发、回归和发布验证 | 终局验收 |

### 10.1 每批次偏航检查

```text
□ 是否新增了一个 Agent Service 或 AgentLoop Owner？
□ 是否让 Runtime 直接访问 Provider/Repository 私有属性？
□ 是否为了迁移新增 Port/Facade/Coordinator/转换层？
□ 是否把 Queue/Command/Compaction 行为塞进 Agent Runtime？
□ 是否修改了客户端协议、Inbox wire 或 Core 源码？
□ 是否留下 deprecated/compatibility 入口？
```

任一答案为“是”，必须暂停该批次并重新划定边界。

---

## 11. 非功能需求

- **可组合**：`ftre-agent` 契约包可独立导入，Runtime 由 Plugin 装载；
- **可卸载**：Runtime、Provider、Hook、Task、线程和连接可逆清理；
- **可测试**：Agent Service 支持 Fake Service，Runtime 支持脱离 Gateway 的契约测试；
- **可观测**：run/session/turn/step 坐标贯穿 Hook 和日志，但不记录 Prompt 全文或凭据；
- **并发安全**：不同 Session 可并行，同一 Session 串行化由 Inbox；
- **可升级**：Core、Runtime、Agent 契约和 Host Service 按稳定契约分别升级；
- **工程卫生**：无空源码目录、缓存、死代码、兼容 alias、重复 DTO 或第二 Composition。
- **独立发行**：`ftre-agent` 不依赖 Host；`ftre-agent-runtime` 不反向 import `ftre.services.*`
  实现，依赖关系可由 wheel 元数据和 AST 扫描证明。
- **行为不变**：F33 只改变代码 Owner、导入路径和装载入口；不改变 Session/Inbox/Client
  wire，不改变 Core Hook 语义，不改变普通消息、Steering、Tool、LLM、压缩和错误恢复行为。

---

## 12. 测试与验收标准

### 12.1 架构验收

- [x] AC1：`packages/ftre-agent` 可以在没有 Gateway 的环境中独立导入。
- [x] AC2：`packages/ftre-agent-runtime` 可以在没有 Host 源码目录的洁净环境中安装，且只有一个 Runtime Provider 和一个 AgentLoop Owner。
- [x] AC3：`src/ftre/services/agent/runtime/` 已删除，不存在旧 Agent Facade 或兼容 alias。
- [x] AC4：Host 通过 Composition 加载 Runtime Plugin，不直接构造 AgentLoop。
- [x] AC5：Runtime 不跨 Owner import Repository、Provider、Registry 或私有 Manager。
- [x] AC6：Agent Service 不依赖 Session Repository、Inbox、Compaction、Command、Channel 或客户端；只接收 `InboundMessage`。
- [x] AC7：Ftre Agent Hook 定义、发布者和监听者有唯一 Owner；Core Hook 不被复制；Hook unload 可逆。
- [x] AC8：Runtime 与 Core 之间没有终局 `core_bridge` 转换层。

### 12.2 功能验收

- [x] AC9：普通 InboundMessage 可以完成一次完整 Agent Turn。
- [x] AC10：Tool Call、Tool Result、Reasoning、LLM Stream 和 Assistant Output 不回归。
- [x] AC11：Steering 在下一次允许的 Hook 边界被消费，且不引入 Queue 到 AgentService。
- [x] AC12：Compaction、Retry、Fallback、Trace 卸载/重启后行为和资源均可逆。
- [x] AC13：Command、Inbox、Channel 仍由各自 Owner 处理，Runtime 不解析接入命令。
- [x] AC14：取消、失败、重试、并发和 in-flight unload 均有稳定结果。

### 12.3 发行验收

- [x] AC15：两个 Agent Package 均有完整 `pyproject.toml`、README、版本元数据和唯一 Runtime `ftre.plugins` entry point；契约包不伪造业务 Plugin entry point。
- [x] AC16：洁净虚拟环境可以分别安装契约包、Runtime 包和 full Host 组合。
- [x] AC17：wheel 不包含 Host 私有源码、测试数据、缓存或临时文件。
- [x] AC18：全量 pytest、Ruff、diff check、Gateway smoke 和生命周期测试通过。
- [x] AC19：架构 AST 扫描确认没有重复 Owner、Service Locator、兼容入口和空目录。
- [x] AC20：PRD、TODO、CHANGELOG、执行报告和提交历史与实际代码一致。
- [x] AC21：AST 与 wheel 元数据证明 Runtime 不反向依赖 `ftre.services.*` 实现，且不存在 Host↔Runtime 循环发行依赖。
- [x] AC22：DSH 对照项已逐项记录；未复制 Agent 内置 Inbox，未复制 Core Hook，未新增无真实消费者的 Factory/Port/DTO。

### 12.4 必须执行的验证命令

F33.6 至少执行以下命令，并在执行报告中保留原始结果：

```powershell
# 1. 契约包脱离 Host 导入
python -c "from ftre_agent import AgentService, InboundMessage; print('agent contract ok')"

# 2. Runtime 包脱离 E:\\ftre\\src 导入
python -c "from ftre_agent_runtime import apply; print('runtime package ok')"

# 3. Host 只从 Runtime Plugin entry point 组装
python -m pytest -q tests/architecture tests/contracts tests/lifecycle tests/startup

# 4. 全量行为与工程卫生
python -m pytest -q
python -m ruff check src tests packages
git diff --check
```

洁净 venv 还必须分别验证：仅安装 `ftre-agent`、仅安装 `ftre-agent-runtime`（加其公开依赖）、
安装完整 ftre Host；三种场景都不得通过工作区源码路径“碰巧成功”。

---

## 13. 终局完成判定

只有同时满足以下条件，才允许将 F33 标记为已验收：

```text
ftre Host
  └─ 不再拥有 AgentLoop/AgentService 的具体实现

ftre-agent
  └─ 只有稳定 Agent 契约

ftre-agent-runtime
  └─ 只有 Runtime 实现和 Provider Plugin

ftre-agent-core
  └─ 只有算法核心

所有业务能力
  └─ 由 Service / Plugin / Package 的唯一 Owner 提供
```

最终必须能够回答：

1. Agent Service 的公开契约在哪里？
2. AgentLoop 的唯一实现在哪里？
3. 谁创建和销毁 Runtime？
4. Runtime 通过哪些 Inject Service 工作？
5. Queue、Command、Compaction、Retry、Fallback 是否仍保持独立？
6. 卸载任意 Plugin 后，哪些行为和资源会消失？

如果任何答案需要解释第二个 Agent Owner、隐式绑定、转换桥或兼容入口，说明 F33 尚未完成。

---

## 14. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-26 | 恢复 F33 终局 Agent Package 架构 PRD，固定 `ftre-agent`、`ftre-agent-runtime`、Host Service、Plugin 和 Core 的边界及目标文件树 | 防止文档误删或 F31/F32/F33 迭代过程中丢失轻内核、Plugin-first 和单一 Owner 目标 |
| 2026-08-26 | 根据 DSH 最新代码复核补充 Agent Registry/Loop Provider 分离、Session durable event 与 live Hook 分工、ftre 独立 Inbox 取舍、实际 F32 Inject、Hook Owner、文件映射、独立发行依赖和 AC21/AC22；修正原先虚构的 `run-start`/`step-completed`/`run-end` 与 Core Hook Owner 描述 | 让 F33 以真实代码和 DSH 可验证结构为依据，避免复制 DSH Inbox、重复 Core Hook 或制造 Host↔Runtime 循环依赖 |
| 2026-08-26 | F33 实现验收：`ftre-agent` 契约包与 `ftre-agent-runtime` Provider 包落地，Host 删除旧 Agent Runtime Owner，Composition 改为 entry point 装载；AC1-AC22 全部通过（全量 pytest 667 passed、ruff、diff check、Gateway smoke、wheel 洁净安装验证） | 固定 Agent 终局分层：契约、Runtime、Host Service、Plugin、Core 各自唯一 Owner |
