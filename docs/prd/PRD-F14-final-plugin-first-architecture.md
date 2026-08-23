# PRD-F14：轻内核 + Plugin-first 最终目标架构

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F14 |
| 名称 | 轻内核 + Plugin-first 最终目标架构 |
| 状态 | 开发中 |
| 创建日期 | 2026-08-24 |
| 定稿日期 | 2026-08-24 |
| 验收日期 | — |
| 前置阶段 | F13（Plugin-first 内核收敛与消息交接） |
| 关联文档 | `AGENTS.md`、`docs/TODO.yaml`、`PRD-F12`、`PRD-F13`、`docs/execution/prompts/F14/` |

## 1. 背景

F1-F13 已经完成旧目录退役、Cordis 生命周期接入、Service Owner 收敛、Hook 语义化、
Command/Agent 解耦、`ftre-inbox` 与 `ftre-compaction` 独立包化，以及 Queue → History
交接。但是当前目录仍保留了重构过程中的中间表达：

- `platform/` 的名字不能直观表达“这里仅允许放内核机制”；
- `features/` 中的能力实际上都是 Plugin，但目录名没有显示其生命周期属性；
- `services/agent_loop/` 看起来像第二个公共 Agent Service，实际只是 Agent 的内部运行时；
- concrete Channel、Command、Trace 和产品 Feature 的 Service/Plugin 身份仍需看代码才能判断；
- `packages/` 的拆分依赖局部需求，尚无统一的“什么时候值得独立发行”门禁；
- Message、Hook、Service、Plugin、Package 等概念已经齐全，但缺少一份长期唯一的终局契约。

F14 不再继续增加架构名词。它的任务是删除重构中间态，让文件树、依赖方向、运行流程和
卸载边界表达同一套架构。

## 2. 总目标

最终架构遵守一句话：

> Cordis Kernel 只提供机制；ftre Host 只保留运行 Agent 所需的稳定 Service；所有产品行为
> 都由 Plugin 贡献；只有能够独立安装、禁用、测试和发布的完整能力才进入 Package。

完成后，开发者只看目录和 Plugin Manifest，就能回答：

1. 这项能力由哪个 Plugin 创建和销毁；
2. 它提供哪个 Service，或监听/发布哪些 Hook；
3. 它依赖哪些公开 Service；
4. 不安装、禁用或卸载它以后，基础系统如何继续运行；
5. 它是仓库内部模块，还是可以独立构建发布的 Python Package。

## 3. 非目标

- 不重写 `cordis-py` 的 Context、Fiber、Effect 或依赖解析算法。
- 不重写 `ftre-agent-core` 的 ReAct、LLM stream 和 Tool call 算法。
- 不把每个目录都拆成 PyPI 包；Package 数量不是架构质量指标。
- 不新增统一 `ServiceBag`、`AgentControlPort`、Coordinator、Facade 或全局 Locator。
- 不建立一个集中收纳所有 DTO 的 `contracts/` 大包；公开协议由产生该语义的 Owner 管理。
- 不为旧导入路径、旧 `features` 路径或旧 `agent_loop` 路径保留兼容 alias。
- 不在本阶段修改 Desktop UI；既有 HTTP/WS wire contract 若必须变化，应另立跨仓库 PRD。
- 不在 F14 中直接发布新的 PyPI 项目；发行仍需独立版本、洁净安装和发布流程。

## 4. 术语与唯一判定规则

### 4.1 Kernel

Kernel 是完全不理解产品业务的运行机制，只允许拥有：

```text
Kernel
├─ Context 中的 Service 注册与依赖解析
├─ Plugin Manifest / Discovery / Loader / Manager
├─ HookSpec 注册、作用域分发、receipt 和取消
├─ Fiber / Effect 生命周期、逆序清理和 in-flight drain
└─ Plugin 状态、缺失依赖、冲突和启动失败诊断
```

Kernel 禁止识别 Session、Agent、Queue、Command、Compaction、Tool、Prompt、MCP、Skill、
Schedule、Team、WebSocket payload 等业务词汇。Kernel 只执行 Hook，不拥有任何业务 Hook 名称；
HookSpec 必须定义在发布该语义边界的 Service 或 Package 中。

### 4.2 Service

Service 是可被多个消费者复用的稳定运行时能力。它可以有状态，必须有窄公开方法和唯一
Service key，但不负责把自己装入全局运行时。

满足下列条件时才建立 Service：

- 至少有两个真实消费者，或它本身是系统的稳定能力边界；
- 需要保存跨调用状态、管理资源或隐藏存储/适配器细节；
- 消费方不应知道它的 Repository、Runtime 或 concrete adapter；
- 它能以少量业务方法描述，而不是暴露内部对象图。

单个纯函数、一次性 Handler、只转发参数的 Facade 不建立 Service。

### 4.3 Plugin

Plugin 是装配与生命周期边界。每项完整业务能力必须有一个唯一 Plugin Owner，负责：

- 声明 `inject` 和 `provide`；
- 创建自己的 Service、Repository、Runtime 和 Adapter；
- 注册 Hook、Tool、Command、Route、Channel 或后台任务；
- 使用 Effect 撤销所有贡献并释放资源。

Provider Plugin 可以提供一个必选 Service；Behavior Plugin 可以只注册行为；一个可选 Plugin
也可以同时提供自己的 Service。Plugin 不是第二套业务对象模型。

### 4.4 Hook

Hook 是“某个 Owner 发布的稳定时机”，用于把可选行为插入执行链。只有调用方不应该知道
监听者是否存在时才使用 Hook。

```text
直接 Service 调用：调用者明确需要某项能力才能完成工作
Hook：             调用者只发布时机，零个或多个 Plugin 可以响应
```

禁止为了隐藏普通函数调用而 Hook 化所有逻辑。控制型 Hook 必须有类型化 Decision、明确
fail-open/fail-closed 语义和有界重试；观察型 Hook 不得偷偷改变主流程。

### 4.5 Package

Package 是安装和发行边界，不是代码分层。一个 Package 内通常包含一个完整 Plugin、可选
Service、自己的模型、持久化、测试和 entry point。

只有同时满足以下条件才允许进入 `packages/`：

1. 不安装它时 `ftre gateway` 或最小 Agent Turn 仍可启动；
2. 它具有独立、完整且可命名的业务能力；
3. 它只依赖 ftre 公共 Service/Hook 契约，不 import Host 私有 Runtime/Repository；
4. 它有唯一 `ftre.plugins` entry point，且 unload/restart 后无残留；
5. 它可以独立构建 wheel、在洁净环境安装并运行自身测试；
6. 它拥有自己的配置、数据迁移和版本兼容责任；
7. 至少存在独立复用、独立发布或按需安装的实际价值。

### 4.6 内部实现

Repository、Adapter、Runtime、Driver、Projection 和数据模型属于 Owner 内部实现，不因类名
不同而升级为新的架构层。它们放在所属 Service/Plugin 目录内，不能被其他能力跨目录 import。

## 5. 最终能力归属

### 5.1 内核机制

| 能力 | 最终 Owner | 说明 |
|---|---|---|
| Context / Fiber / Effect | `cordis-py` | 外部成熟运行时，ftre 不维护 fallback |
| Hook Runtime | `ftre.kernel.hooks` | 只负责注册、分发、scope、receipt、取消 |
| Plugin Runtime | `ftre.kernel.plugins` | Manifest、发现、加载、状态和清理 |
| Diagnostics | `ftre.kernel.plugins` | 诊断 Plugin/Fiber，不诊断具体业务数据 |

### 5.2 Host 稳定 Service

| Service | Service key | 负责 | 明确不负责 |
|---|---|---|---|
| ConfigService | `config` | 配置快照、revision、原子更新 | 各 Feature 的业务默认值 |
| FilesystemService | `filesystem` | 路径策略、安全 IO | Workspace 业务选择 |
| HttpService | `http` | Router 贡献、冲突、冻结、dispose | 业务 Route 实现 |
| SessionService | `sessions` | Session 身份、正式消息历史、持久化 | pending 队列、Agent active Turn |
| AgentService | `agents` | `InboundMessage → TurnOutcome`、active Turn、取消 | Queue、Command、Compaction、Channel |
| LlmService | `llm` | 模型适配、请求和 stream | Prompt 产品行为、Agent 队列 |
| ToolService | `tools` | Tool 注册、scope、执行视图 | 静态拥有全部具体 Tool |
| SystemPromptService | `system_prompt` | 结构化 section 注册和组装 | 在自身写死 Feature 文本 |
| MessageBusService | `message_bus` | transport-neutral inbound/outbound 分发 | 持久化、队列、命令实现 |
| ChannelService | `channels` | Channel 注册、启停和发送 | 具体 WebSocket/Subagent 行为 |
| WorkspaceService | `workspaces` | Session 工作区与路径策略组合 | 通用文件 IO |
| AttachmentService | `attachments` | 附件保存、读取、MIME 与安全边界 | 聊天消息历史 |

`AgentService` 是唯一公开 Agent 能力。现有 `AgentLoop`、Driver、TurnExecutor、
CompletionRegistry 都降为 `AgentService` 目录下的私有 Runtime，不再形成第二个 Service key、
第二个 Plugin Owner 或顶层 `agent_loop/` 概念。

### 5.3 内置 Plugin

| Plugin | 形态 | 默认策略 | 缺失时行为 |
|---|---|---|---|
| Command | 自带 `CommandService` + inbound Hook | 默认启用 | 普通 Agent 输入仍可运行，slash command 返回 unsupported |
| WebSocket Channel | concrete Channel adapter | Gateway 必选 | Desktop Gateway 不开放 WS；嵌入式 Agent 不受影响 |
| Subagent Channel | concrete Channel adapter | 默认启用 | 不支持跨 Agent channel 发送 |
| Session Title | Session post-commit / Prompt Hook | 可选 | 使用调用方标题或无自动标题 |
| Context Govern | Prompt section Plugin | 默认启用 | 不注入工作区治理提示 |
| Plan | Tool/Hook Plugin | 可选 | 无计划行为 |
| Trace | 自带 TraceService + exporter + Router | 可选 | 不持久化/查询 trace，Agent Turn 正常 |
| MCP | 自带连接 Service + Tool/Router | 可选 | 无 MCP 工具 |
| Skill | 自带 catalog Service + Tool/Router | 可选 | 无 Skill catalog |
| Schedule | 自带 store/service/task/channel/tool/router | 可选 | 无定时任务 |
| Team | 自带 team state/service/tools | 可选 | 无多 Agent 团队行为 |

“内置”仅表示由 ftre 仓库维护并可出现在默认 Composition，不表示绕过 Plugin Loader。

### 5.4 独立 Package

| Package | 当前/最终职责 | 与 Host 的边界 |
|---|---|---|
| `ftre-inbox` | QueueItem、next-turn/next-step、持久化、claim、worker、queue wire | claim 后只把 `InboundMessage` 交给 `AgentService` |
| `ftre-compaction` | CompactionService、压缩策略、命令和 Agent/Inbox Hook | Host 不 import、不持有、不提供 no-op fallback |

MCP、Skill、Schedule、Team 是下一批“可包化候选”，但 F14 只要求它们先成为边界完整的内置
Plugin。只有独立构建和实际复用需求成立后再迁入 `packages/`，迁移时不得改变 Service key、
HookSpec 或消费方式。

以下能力明确保留在 Host，不进行包化：Config、Filesystem、HTTP registry、Session、Agent、
LLM、Tool registry、System Prompt registry、MessageBus、Channel registry、Workspace、Attachment。
它们共同构成 ftre Gateway 的产品骨架，拆包只会增加版本联动，并不会获得独立使用价值。

## 6. 最终目标文件树

```text
E:/ftre/
├─ pyproject.toml                         # ftre Host；可选 extras，不硬依赖可选 Package
├─ README.md
├─ AGENTS.md
├─ docs/
│  ├─ prd/
│  ├─ execution/
│  └─ TODO.yaml
│
├─ src/
│  └─ ftre/
│     ├─ __init__.py
│     ├─ main.py                          # Typer CLI；只选择宿主命令
│     │
│     ├─ app/                             # 进程/宿主边界
│     │  ├─ cli/
│     │  └─ gateway/
│     │     ├─ bootstrap.py               # 启停 Gateway，不构造业务对象图
│     │     ├─ composition.py             # 唯一默认 Plugin 清单
│     │     └─ http/                      # FastAPI/uvicorn Host 物化
│     │
│     ├─ kernel/                          # 业务零知识机制层
│     │  ├─ hooks/
│     │  │  ├─ runtime.py                 # 注册、dispatch、in-flight drain
│     │  │  ├─ spec.py                    # 通用 HookSpec 机制，不放业务 Hook 名
│     │  │  ├─ scope.py
│     │  │  └─ receipt.py
│     │  └─ plugins/
│     │     ├─ manifest.py
│     │     ├─ discovery.py
│     │     ├─ catalog.py
│     │     ├─ loader.py
│     │     ├─ manager.py
│     │     └─ diagnostics.py
│     │
│     ├─ services/                        # Host 稳定公共能力
│     │  ├─ config/
│     │  ├─ filesystem/
│     │  ├─ http/
│     │  ├─ session/
│     │  │  ├─ models.py
│     │  │  ├─ hooks.py                   # session/* 语义由 Session Owner 定义
│     │  │  ├─ service.py
│     │  │  ├─ plugin.py
│     │  │  ├─ persistence/               # Session 私有 Repository/Store
│     │  │  └─ projection/
│     │  ├─ agent/
│     │  │  ├─ models.py                  # InboundMessage / TurnOutcome
│     │  │  ├─ hooks.py                   # agent/* 语义边界
│     │  │  ├─ service.py                 # 唯一公开 Agent Service
│     │  │  ├─ plugin.py                  # 唯一 Agent Provider Plugin
│     │  │  ├─ profiles/
│     │  │  └─ runtime/                   # 私有执行细节，不提供 agent_loop Service
│     │  │     ├─ driver.py
│     │  │     ├─ turn_executor.py
│     │  │     ├─ completion.py
│     │  │     └─ core_adapter.py          # 对 ftre-agent-core 的最薄适配
│     │  ├─ llm/
│     │  ├─ tools/
│     │  ├─ system_prompt/
│     │  ├─ messaging/
│     │  │  ├─ hooks.py                   # messaging/inbound 等业务 HookSpec
│     │  │  ├─ bus/                       # transport-neutral event plane
│     │  │  └─ channels/                  # Channel registry/base contract
│     │  ├─ workspace/
│     │  └─ attachment/
│     │
│     └─ plugins/                         # 一眼可见的产品行为与 concrete adapter
│        └─ builtin/
│           ├─ command/
│           ├─ channels/
│           │  ├─ websocket/
│           │  └─ subagent/
│           ├─ session_title/
│           ├─ context_govern/
│           ├─ plan/
│           ├─ trace/
│           ├─ mcp/
│           ├─ skill/
│           ├─ schedule/
│           └─ team/
│
├─ packages/                              # 可独立安装/发布的完整能力
│  ├─ ftre-inbox/
│  │  ├─ pyproject.toml
│  │  ├─ src/ftre_inbox/
│  │  │  ├─ plugin.py                     # 唯一 ftre.plugins 入口
│  │  │  ├─ service.py
│  │  │  ├─ hooks.py
│  │  │  ├─ models.py
│  │  │  ├─ worker.py
│  │  │  ├─ persistence/
│  │  │  └─ wire/
│  │  └─ tests/
│  └─ ftre-compaction/
│     ├─ pyproject.toml
│     ├─ src/ftre_compaction/
│     │  ├─ plugin.py                     # Service、Hook、Command 的唯一 Owner
│     │  ├─ service.py
│     │  ├─ hooks.py
│     │  ├─ commands.py
│     │  ├─ config.py
│     │  └─ models.py
│     └─ tests/
│
└─ tests/
   ├─ architecture/                       # 依赖方向、Owner、禁止导入
   ├─ contracts/                          # Service/Hook/wire 契约
   ├─ lifecycle/                          # load/unload/restart/in-flight
   ├─ startup/                            # 最小/默认/缺失 Package Composition
   └─ integration/                        # Gateway、WS、Session、Agent 真实链路
```

### 6.1 文件树的强制解释

- `kernel/` 取代含义宽泛的 `platform/`，且不能放任何业务类型。
- `plugins/builtin/` 取代 `features/`；目录本身直接表达“这些能力可由 Plugin 生命周期管理”。
- `services/agent/runtime/` 取代顶层 `services/agent_loop/`；Loop 是 AgentService 的内部实现，
  不是与 AgentService 并列的能力。
- concrete WebSocket/Subagent 位于 Plugin，而 Channel base/registry 位于 Service。
- Plugin 自带的 Service 跟随 Plugin 放置，例如 ScheduleService 位于 Schedule Plugin 内；只有
  Host 稳定能力才进入顶层 `services/`。
- 不建立 `infrastructure/` 垃圾桶；Repository/Adapter 跟随唯一 Owner 放置。

## 7. 最终依赖方向

```text
app/gateway
    │  只声明 Manifest、启动 Host
    v
kernel/plugins ───────────────> cordis-py
    │ load
    ├────────> Host Provider Plugins ─────> services/*
    ├────────> builtin Behavior Plugins ──> Inject Service / register Hook
    └────────> external Package Plugins ──> Inject Service / register Hook

services/*
    └────────> 自己目录内的 model/runtime/repository/adapter

plugins/* 与 packages/*
    └────────> 只依赖 Owner 公开的 Service、model、HookSpec
```

禁止方向：

- Kernel → Service/Plugin/Package；
- Service → concrete Plugin；
- Plugin A → Plugin B 的私有实现；
- Host → `ftre-inbox`/`ftre-compaction` 的具体类；
- 业务代码 → Composition、PluginManager 或 Context 进行运行时 Service Locator；
- Agent Runtime → QueueItem、Command parser、CompactionService 或 WebSocket payload。

## 8. 最终消息与 Agent 数据流

```text
WebSocket / HTTP / Subagent / Schedule
                  │
                  v
          MessageBusService
                  │
                  v
       messaging/inbound Hook
          │                 │
          │ slash command   │ ordinary input
          v                 v
   Command Plugin      Inbox Package
   CommandService      durable admission
          │                 │
          │                 ├─ queue snapshot / ACK
          │                 └─ worker → claim
          │                              │
          └─ result/event                v
                               AgentService.run(
                                  InboundMessage
                               )
                                      │
                         SessionService 写正式历史
                                      │
                         agent/before-turn Hook
                                      │
                             private Agent runtime
                                      │
                    ┌─────────────────┴─────────────────┐
                    v                                   v
              LlmService                           ToolService
                    │                                   │
                    └──────── Session events ──────────┘
                                      │
                         agent/after-turn Hook
                                      │
                       MessageBus → Channel outbound
```

关键语义：

- `messaging/inbound` HookSpec 由 Messaging Owner 定义，Kernel 不认识它；
- Command Plugin 只消费命令，不能创建 Agent Turn；
- Inbox Package 只拥有 pending/claim/worker，不进入 Agent Runtime；
- AgentService 只接收 `InboundMessage`，并且不知道消息来源；
- Compaction 仅通过 Inbox/Agent/LLM Hook 和公开 Service 工作；不加载 Compaction 时链路自然为空；
- Schedule、Team、Tool 等若需要异步 Agent 消费，只调用公开 InboxService；若缺失则返回明确
  capability error，不直接调用私有 Runtime 伪造队列语义。

## 9. Composition 规则

### 9.1 Composition Root 允许做什么

- 建立根 Context 与唯一 HookRuntime；
- 创建 PluginManager；
- 声明有序 Manifest 清单及 required/default_enabled；
- 读取启动配置并调用 PluginManager；
- 在关闭时逆序 dispose。

### 9.2 Composition Root 禁止做什么

- `new` SessionService、AgentService、Repository、Channel 或 Feature Service；
- 手工调用 `bind_*` 把一个 Service 塞进另一个 Service；
- 手工注册业务 Router、Hook、Tool、Command 或 Channel；
- 为可选 Package import fallback/no-op 实现；
- 保存第二份可变 Service 引用表。

### 9.3 默认与最小 Composition

```text
最小 Agent Composition
  = Kernel + Config + Session + Agent + LLM + Tools + SystemPrompt

Gateway Composition
  = 最小 Agent Composition
  + Filesystem + HTTP + MessageBus + Channel registry + Workspace + Attachment
  + Gateway 所需 concrete Channel Plugins

默认完整 Composition
  = Gateway Composition
  + Command + Inbox + Compaction
  + 默认启用的 builtin behavior Plugins
```

最小 Composition 不要求 Inbox、Compaction、MCP、Skill、Schedule、Team、Trace 或 WebSocket。

## 10. 配置与发行

- `ftre` 主发行物不得硬依赖 `ftre-inbox`、`ftre-compaction` 或未来可选 Package；
- 可选发行物通过 extras 表达，例如 `ftre[inbox]`、`ftre[compaction]`、`ftre[full]`；
- Plugin 通过 `ftre.plugins` entry point 或显式本地插件目录发现；
- 外部 Plugin 默认不自动启用，必须经过配置 allowlist；
- Service key、Plugin id、HookSpec 名和 wire event 是稳定兼容面；私有文件路径不是兼容面；
- 删除旧目录时不提供 alias；调用方必须在同一阶段迁移到稳定公开入口。

## 11. 功能需求

- [ ] **FR1：终局目录收敛**
  - `platform/ → kernel/`、`features/ → plugins/builtin/`；
  - `services/agent_loop/ → services/agent/runtime/`；
  - 删除旧目录、兼容导出、空 `__init__` 聚合和陈旧文档路径。

- [ ] **FR2：Kernel 业务零知识**
  - Kernel 只实现第 4.1 节机制；
  - 业务 HookSpec 与业务模型由各自 Owner 定义；
  - 架构门禁阻止业务 import 和业务词汇回流。

- [ ] **FR3：唯一 Agent Service**
  - `agents` 是唯一公开 Agent key；
  - AgentService 只执行 `InboundMessage`、管理 active Turn 和取消；
  - AgentLoop/TurnExecutor/CompletionRegistry 仅为私有 Runtime，不被业务 Plugin import。

- [ ] **FR4：消息接入 Plugin 化**
  - MessageBus 发布 transport-neutral inbound Hook；
  - Command 与 Inbox 分别消费适用输入；
  - Agent Runtime 不进行 Command/Queue 分流；
  - 无消费者时返回稳定 capability error。

- [ ] **FR5：Host Service 收敛**
  - 第 5.2 节 Service 均有唯一 Provider Plugin、Service key 和 Effect；
  - Service 只持有 Inject 的公开依赖，不使用 Context Locator；
  - Repository/Adapter/Runtime 不跨 Owner 泄漏。

- [ ] **FR6：内置能力 Plugin 化**
  - 第 5.3 节每项能力有唯一 Plugin Owner；
  - Service、Route、Hook、Tool、Task、Channel 和资源均随 Plugin 可逆清理；
  - 不加载能力时行为与表中降级语义一致。

- [ ] **FR7：独立 Package 边界**
  - `ftre-inbox` 与 `ftre-compaction` 满足第 4.5 节全部门禁；
  - Host 无 concrete import、无 no-op fallback、无隐式 hard dependency；
  - wheel 构建、洁净安装、禁用、卸载、restart 均可验证。

- [ ] **FR8：Hook 所有权和语义**
  - Kernel 只提供 Hook 机制；
  - Agent、Session、Tool、Prompt、Messaging、Inbox 分别拥有自己的 HookSpec；
  - 控制型 Hook 有类型化 Decision、故障策略和有界重试；
  - 直接必选协作仍使用 Service，不为“看起来解耦”滥用 Hook。

- [ ] **FR9：Inject/Provide 唯一跨能力依赖**
  - Plugin 声明依赖并创建自己的实现；
  - 业务代码不从 AgentLoop、Composition、PluginManager、全局变量反查 Service；
  - 禁止单实现 Port、透传 Facade、Coordinator 和重复 Owner。

- [ ] **FR10：配置与发现**
  - required、default_enabled、配置来源和缺失能力语义明确；
  - 外部 Plugin 显式 allowlist；
  - extras 与 entry point 支持 Host、最小安装和完整安装。

- [ ] **FR11：生命周期完整性**
  - load/unload/restart、依赖 pending/recovery 和 in-flight drain 行为可验证；
  - Route、Hook、Task、Thread、Listener、Channel、数据库和文件 watcher 无残留；
  - restart 后消费者只解析当前 Service 实例，不持有旧闭包。

- [ ] **FR12：可理解性与文档**
  - 目标树、能力表、依赖图和默认 Composition 与代码一致；
  - 每个公共 Service/Plugin 和非显然生命周期代码有中文边界注释；
  - 架构执行报告列出迁移映射、删除项、保留项和 Package 审计结论。

## 12. 实施阶段

本节各阶段的可直接执行 AI 提示词位于
[`docs/execution/prompts/F14/`](../execution/prompts/F14/README.md)。提示词必须按编号串行使用，
且只有在本 PRD 状态进入 `approved`/`开发中` 后才允许执行。

### F14.1 终局目录与依赖基线

- 生成当前 Service/Plugin/Hook/Package Owner 清单；
- 冻结第 6-9 节目标和 import allowlist；
- 先建立会在迁移期间持续运行的架构测试。

### F14.2 Kernel 命名与业务零知识迁移

- 将 `platform` 收敛为 `kernel`；
- 迁移 Hook Runtime 与 Plugin Runtime 公共入口；
- 同一切片迁移全部生产/测试引用并删除旧路径，不保留 alias。

### F14.3 Agent Runtime 内聚

- 将 `services/agent_loop` 内部实现归入 `services/agent/runtime`；
- 合并 Agent Provider Owner，确保只 provide `agents`；
- 删除 Loop 的公开 Service 身份、跨模块私有引用和重复 Driver DTO。

### F14.4 Messaging Ingress 收敛

- 由 Messaging Owner 定义 transport-neutral inbound HookSpec；
- Command Plugin 与 Inbox Package 接入同一公开时机；
- 从 Agent Runtime 删除 Command/Inbox 分流和可选 Service 绑定。

### F14.5 Builtin Plugin 目录迁移

- 将 `features`、concrete Channel、Session Title、Trace 和 Command 迁入
  `plugins/builtin`；
- Service key 与 wire contract 保持稳定；
- 删除原路径、重复 `plugin.py` Owner 和兼容导出。

### F14.6 Host Service 边界收敛

- 逐项核对第 5.2 节 Service 的公开 API、Provider、inject 和 Effect；
- 私有 Repository/Adapter/Runtime 跟随 Owner 归位；
- 删除 Service Bag、Context Locator、setter、跨 Owner 私有 import 和无价值中间层。

### F14.7 Package 与发行门禁

- 对 `ftre-inbox`、`ftre-compaction` 执行独立构建、洁净安装和 Host 无包启动；
- 将可选依赖改为 extras/entry point；
- 审计 MCP、Skill、Schedule、Team，只记录是否达到下一阶段包化条件，不强拆包。

### F14.8 生命周期、故障与最小 Composition

- 覆盖 Plugin load/unload/restart、in-flight Hook、缺失依赖恢复；
- 覆盖无 Inbox、无 Compaction、无 Command 和最小 Agent Turn；
- 覆盖 Gateway stop、Channel 断连、Package restart 后旧实例释放。

### F14.9 债务与生成物清理

- 全盘扫描死代码、兼容壳、重复 Owner、陈旧 import、缓存、空目录和文档旧路径；
- 删除迁移临时脚本和不再表达真实架构的测试替身；
- 确认源码中不存在 `platform`、`features`、`agent_loop` 旧生产路径。

### F14.10 最终验收与执行报告

- 全量 pytest、ruff、diff check、构建、洁净安装和 Gateway smoke；
- 按 AC 逐条留存命令、结果和人工核对证据；
- 同步 PRD、TODO、AGENTS.md、README、CHANGELOG 和执行报告。

## 13. 验收标准

- [ ] **AC1**：仓库实际目录与第 6 节目标树一致；生产代码不存在旧 `platform`、`features`、
  `services.agent_loop` import 或兼容 alias。
- [ ] **AC2**：架构测试证明 Kernel 对业务实现零依赖，且 Kernel 中不存在业务 HookSpec 名称。
- [ ] **AC3**：Composition Root 只建立 Kernel 骨架、声明 Manifest 和启动/关闭 PluginManager；
  不实例化业务 Service、不注册业务贡献。
- [ ] **AC4**：只有 `agents` 一个 Agent Service key；AgentService/私有 Runtime 不包含 QueueItem、
  pending、Command parser、CompactionService 或 WebSocket payload。
- [ ] **AC5**：普通输入、slash command、无 Inbox、无 Command 四条接入路径均符合第 8 节，
  且 Command 不创建 Turn、Inbox claim 后只交付 `InboundMessage`。
- [ ] **AC6**：第 5.2 节每个 Host Service 只有一个 Provider Plugin；跨能力依赖均可从
  `inject` 声明追踪，没有 Context Locator、全局 setter 或私有实现 import。
- [ ] **AC7**：第 5.3 节每个内置 Plugin 均可单独 unload/restart；对应 Hook、Route、Task、
  Channel、Tool、Service 和资源完整消失，基础 Turn 按缺失语义继续运行。
- [ ] **AC8**：`ftre-inbox`、`ftre-compaction` 可分别构建 wheel、在洁净环境安装和运行测试；
  Host 未安装二者仍可导入、启动最小 Composition 和完成直接 Agent Turn。
- [ ] **AC9**：HookSpec 均由语义 Owner 定义；控制型 Hook 的 Decision、错误策略和重试边界有
  契约测试，不存在重复旧 Hook 或字符串散落注册。
- [ ] **AC10**：最小 Agent Composition、Gateway Composition、默认完整 Composition 均有启动/
  关闭测试，关闭后无 pending Task、旧 Service 闭包、监听端口或数据库句柄。
- [ ] **AC11**：全盘扫描无死代码、兼容壳、重复 Owner、空目录、`__pycache__`、`.pyc` 和
  迁移临时文件；`git diff --check` 通过。
- [ ] **AC12**：`python -m pytest -q`、`python -m ruff check src tests packages`、各 Package
  独立测试、wheel build、洁净安装和 Gateway smoke 全部通过，执行报告记录实际结果。
- [ ] **AC13**：AGENTS.md、README、Service/Plugin 文档和 Composition 清单与最终树一致；每个
  公共能力均可用“Owner / Service 或 Hook / inject / 缺失行为”四项说明。

## 14. 测试计划

### 14.1 架构测试

- import 分层 allowlist 与 forbidden paths；
- Kernel 业务词汇和业务依赖扫描；
- provide key 唯一性与 Manifest/Provider 一致性；
- Plugin 跨私有实现 import 扫描；
- Agent Runtime 禁止 Queue/Command/Compaction/Channel 类型；
- 禁止 Port/Coordinator/Facade/global setter/compat alias。

### 14.2 契约测试

- `InboundMessage → TurnOutcome`；
- messaging inbound 的 Command/Inbox/unhandled 决策；
- Session、Agent、Tool、Prompt、Messaging、Inbox HookSpec；
- request_id 的 queue → history 相关性；
- Service key、Plugin id、entry point 和配置错误。

### 14.3 生命周期测试

- 每个 Plugin 的 load/unload/restart；
- 依赖缺失进入 pending，依赖恢复后重新激活；
- in-flight Hook/Turn/Tool/LLM stream 的取消与 drain；
- Channel、Route、Task、Thread、Watcher、SQLite/JSON store 清理；
- Package restart 后所有消费者使用新实例。

### 14.4 集成与发行测试

- 最小 Agent Composition 完成真实或可控 LLM Turn；
- Gateway WS admission、Command、Inbox、Session history 和 outbound；
- 无 Inbox/Compaction/Command 的降级路径；
- Package wheel build、洁净 venv 安装、entry point discovery；
- Gateway 启动、连接、消息、取消、关闭 smoke。

## 15. 风险与取舍

| 风险/取舍 | 决策 |
|---|---|
| 一次目录迁移产生大量 diff | 按 F14.2-F14.6 单 Owner 切片迁移；每片立即删除旧路径并跑门禁 |
| 把 Plugin-first 误解为“全部放 packages” | Plugin 是生命周期边界，Package 需额外通过第 4.5 节发行门禁 |
| `plugins/builtin` 中存在自带 Service 的能力 | 允许；Service 跟随唯一 Plugin Owner，不要求所有 Service 都放 `services/` |
| MessageBus 与 Hook 职责重叠 | MessageBus 拥有业务 envelope 和收发；HookRuntime 只执行由 Messaging 定义的扩展时机 |
| AgentService 过大 | 公共面保持单一，内部 Runtime 按 driver/executor/adapter 分文件；内部模块不升级为公共 Service |
| 为避免 import 耦合新增中央 contracts 包 | 协议跟随语义 Owner；出现两个真实跨发行物消费者后再评估独立 SDK |
| 可选包未安装导致默认功能缺失 | Composition diagnostics 明确显示 capability 状态；最小链路不得 import 崩溃 |
| 迁移期间双目录共存 | 不保留兼容期；同一切片同步改完生产、测试、文档并删除旧路径 |

## 16. 完成定义

F14 只有在以下事实同时成立时才可以标记“已验收”：

1. 实际文件树已经达到第 6 节，而不是仅新增一层包装；
2. Kernel、Host Service、Builtin Plugin、Independent Package 四类边界均有自动化门禁；
3. `AgentService`、Inbox、Compaction、Command 的责任可以分别关闭而不互相冒充；
4. 旧路径、旧 Owner、兼容壳和第二实现已删除；
5. 全量测试、生命周期、独立包、洁净安装和 Gateway smoke 有可复现证据；
6. 文档与代码一致，执行报告如实记录未完成项，不以 AC 勾选掩盖 FR 未完成。

## 17. 变更记录

| 日期 | 变更内容 | 理由 | 受影响验收 |
|---|---|---|---|
| 2026-08-24 | 创建最终目标架构草案，冻结 Kernel/Service/Plugin/Hook/Package 判定、目标文件树、依赖方向、数据流和迁移门禁 | F13 已完成阶段性收敛，但仓库仍存在 `platform/features/agent_loop` 等中间表达，需要一份长期终局契约指导后续迁移 | AC1-AC13 待评审 |
| 2026-08-24 | 新增七批串行 AI 执行提示词，覆盖前置检查、代码迁移、中文注释、测试、债务清理、工程卫生、提交和最终交接 | F14 涉及多个高冲突目录，必须让每批任务自包含、可验证且不留下双实现 | AC1-AC13 不变；执行方式细化 |
| 2026-08-24 | 用户授权按七批提示词进入开发，PRD 状态由草稿推进为开发中；F14.1 先建立 Owner、依赖和债务基线 | 开始执行完整 F14，不跳过前置审计 | AC1-AC13 待逐批验证 |
| 2026-08-24 | 完成 F14.1：建立 Manifest/Service/Hook/Package Owner 清单、目标映射、债务基线和架构扫描；提交 `5501fc1`，全量测试 439 passed、ruff 通过 | 为后续路径迁移提供可复现的事实基线和防回归门禁 | AC1、AC3、AC6、AC8 的基线证据已建立，终局 AC 仍待后续批次 |
| 2026-08-24 | 完成 F14.2：`platform → kernel`、`plugin_runtime → kernel/plugins`，删除 Kernel 业务 Hook 名称目录并将名称归还各语义 Owner；全量测试 445 passed、ruff 通过 | 让 Kernel 真正业务零知识，避免继续形成中央 Hook 名称表 | AC2、AC9 已部分验证；AC1、AC12 待后续目录/发行门禁 |
