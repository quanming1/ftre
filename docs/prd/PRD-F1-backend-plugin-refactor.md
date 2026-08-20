# PRD-F1-基于 cordis-py 的后端插件化重构

> 本文档由 `docs/prd/访谈.md` 的架构访谈和现有代码审查收敛而成。用户已授权按整份 PRD 执行，本文在 F1 开工前完成批准并冻结；后续变更必须追加变更记录并重核受影响 AC。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F1 |
| 名称 | 基于 cordis-py 的后端插件化重构 |
| 状态 | approved |
| 创建日期 | 2026-08-20 |
| 定稿日期 | 2026-08-20 |
| 验收日期 | — |
| 关联文档 | `docs/TODO.yaml` 阶段 F1；`AGENTS.md`；`docs/PROCESS.md`；`docs/prd/访谈.md`；`docs/audits/AUDIT-001-ftre-architecture-validation.md` |
| 技术基座 | Python 3.12；`cordis-py 0.4.x`；FastAPI；uvicorn |

## 1. 背景与目标

### 1.1 背景

ftre 已具备 Session、AgentLoop、Channel、Tool、Command、MCP、Skill、Schedule、Team 和外部 Plugin 等能力，但现有后端结构主要通过手工装配和自研插件内核运转，代码所有权与生命周期边界不清楚。

代码审查确认的主要问题如下：

| 现状 | 问题 |
|---|---|
| `src/ftre/main.py` 手工创建绝大多数对象并维护关闭顺序 | Composition Root、CLI 和业务生命周期混在一起 |
| `src/ftre/plugin/kernel/` 自研 Context、EventHub、Registry、Lifecycle、Service | 与计划采用的 `cordis-py` 重复，形成两套容器和两套语义 |
| 根包平铺 `agent/api/bus/channel/command/gateway/mcp/plugin/session/tools` 等目录 | 能力、入口、插件来源与基础设施角色混在同一级 |
| `plugin/builtin` 集中 Skill、MCP、Plan、Team、标题和上下文治理 | `builtin` 是交付来源，不是能力归属 |
| `api/routes.py` 使用模块全局变量和 setter 注入依赖 | 隐式依赖，测试隔离和生命周期清理困难 |
| `ws_channel.py` 同时管理 Channel、FastAPI、WebSocket、CORS、Router、uvicorn 和附件 | Transport、Host 与业务能力耦合 |
| `agent_manager.py` 同时管理 Profile、配置合并、Prompt、Tool 裁剪和 Agent 构造 | 数据管理与运行时构造职责混合 |
| `tools/cron.py`、`skill_plugin.py`、`mcp_plugin.py` 等大文件包含 Store、Service、Tool、Router 和生命周期 | 一个功能被角色目录拆散，同时单文件又承担多个职责 |
| Plugin 扫描结果会影响默认装载，发现与启用没有严格分离 | 未显式启用的外部代码可能进入运行时 |
| `config.py` 存在 `DEFAULT_CONFIG = load_config()` 导入副作用 | 配置存在多个事实源，无法由统一 Service 管理 |

PRD 展开时的质量基线（2026-08-20）：

- `python -m pytest -q`：348 passed，存在 1 个既有 `TestChannel` 收集警告。
- `python -m ruff check src tests`：271 个既有错误。F1 必须区分历史问题与重构新增问题，但最终验收仍以零错误为准。

### 1.2 目标

本阶段完成后的可交付状态：

> ftre 保持单一 Python Distribution，在不改变现有产品协议与 Agent 核心语义的前提下，以 `cordis-py` 作为唯一插件生命周期和响应式依赖注入基座，建立清晰的四层目录、唯一 Composition Root、显式 Plugin 装载以及可撤销的 Service/Tool/Command/Hook/后台任务注册。

具体目标：

1. 用 `cordis-py` 的 Context、Fiber、Service、Inject 和 Effect 替换 `src/ftre/plugin/kernel` 中重复的框架能力。
2. 将根包收敛为 `app / platform / services / features` 四个导航层。
3. 明确 Capability、Service、Provider Plugin、Consumer Plugin、Server 和 Factory 的区别。
4. 将启动装配从 `main.py` 移入唯一 Composition Root，并由根 Context 统一释放资源。
5. 保持 EventBus 业务消息数据面与 Cordis 插件控制面相互独立。
6. 保持 Session 数据、`~/.ftre/config.json`、HTTP/WS 协议、AgentLoop 并发不变量和现有工具语义兼容。
7. 为未来拆成独立 Python 包保留边界，但本阶段不实施多包发布。

### 1.3 已确定的架构决策

| 编号 | 决策 |
|---|---|
| AD1 | 第一阶段保持一个 `pyproject.toml` 和一个 `ftre` Distribution，不复制 DSH 的 219 个 Package 规模。 |
| AD2 | 根包采用 `app / platform / services / features` 四层；能力内部再用 `service.py/plugin.py/server.py/runtime/` 表达角色。 |
| AD3 | Service、Plugin 和 Server 是正交角色：Service 定义调用面，Plugin 管理装卸生命周期，Server 持有网络资源。 |
| AD4 | `cordis-py` 是唯一插件框架；ftre 只保留 Manifest、Catalog、Discovery、Loader、Manager 和 Diagnostics 等项目级薄适配。 |
| AD5 | `EventBus/message_bus` 继续承担 Channel → AgentLoop 的业务消息数据面；Cordis Event 只承担进程内扩展和生命周期控制面。 |
| AD6 | 内置默认能力由 Composition 明确列出；外部插件“可发现”不等于“已启用”。 |
| AD7 | 第一阶段保持 `~/.ftre/config.json` 格式，不迁移到 TOML。 |
| AD8 | Router 在启动 Composition 阶段注册；HTTP Server 最后启动。运行时 Router 热增删留到后续阶段。 |
| AD9 | `AgentService` 是外部调用面；Factory、Loop、Mailbox 和 Compaction 都是默认 Provider 的内部实现。 |
| AD10 | DSH 只作为 Group/Unit、Service/Provider/Consumer 和 Composition 思路参考，不照搬 TypeScript Monorepo 结构。 |
| AD11 | F1 建设的是 **Backend Behavior/Provider Plugin 架构**；Desktop Client Extension 和 pre-boot Recovery Extension 是不同架构层，不以普通 `plugin.py` 冒充支持。 |
| AD12 | Service 不只提供调用方法，还必须提供必要的 scope、source、owner 和诊断信息，使第三方 Plugin 不需要反向读取 Provider 私有状态。 |

### 1.4 非目标

本阶段明确不做：

1. 不拆成多个独立 Python Distribution，不引入 Monorepo 多包版本治理。
2. 不实现 Marketplace、远程下载、签名验证或 Python Entry Point 自动安装。
3. 不实现源码 HMR，也不承诺运行时动态增删 FastAPI Router。
4. 不实现 Plugin 进程隔离、权限沙箱或崩溃自动拉起。
5. 不实现完整 DSH Preset/Isolate 组合体系和 per-agent 独立 Plugin Tree。
6. 不重写 AgentLoop、SessionLane、Mailbox、Compaction、TurnExecutor 的核心算法。
7. 不替换 JSON Session 持久化，不修改现有 HTTP/WS 消息协议。
8. 不重构 `ftre-agent-core` 的算法实现。
9. 不把外部 Octo Plugin 源码移动进 ftre 主仓库。
10. 不迁移 `dsh-vision-toolkit`、`dsh-context-doctor` 或 `dsh-undo-savepoint`；它们仅作为架构审计样本。
11. 不实现 Desktop Client Plugin Manifest、动态 Settings Page、Chat Slot 或 Tool Renderer。
12. 不实现通用 CredentialService、Llm Adapter Registry、托管 Python 下载或 Plugin 自更新。
13. 不实现完整 Safe Mode、离线恢复和灾难恢复 UI；仅允许 Composition Root 保留未来可插入的最小 preflight 边界。
14. 不以“支持后端 Plugin”为由宣称已经支持跨 Backend/Desktop/Worker 的完整 Extension Bundle。

### 1.5 F1 的架构责任边界

F1 对以下场景负责：

- 一个 Python Plugin 通过 Cordis 声明依赖，提供或消费后端 Service。
- Plugin 注册 Tool、Command、Prompt Section、Hook/Event、Channel、Router 和后台资源，并能随 Fiber 清理。
- Plugin 在 global 或 agent scope 中读取实际可见的 Tool、Skill 和 Prompt Contribution。
- Plugin 通过公开 Service 使用 Session、Agent、Workspace、Filesystem、Config 和 HTTP，不导入其他能力的私有 Provider。
- Gateway 能在启动前判断 required Plugin 是否就绪，并在失败时不对外监听。

F1 不对以下场景负责：

- 从第三方包动态装载 React/Electron 代码。
- 在普通 Plugin Loader 已经无法启动时执行完整恢复业务。
- 运行任意不受信 Plugin 的进程级权限隔离。
- 为单个插件增加专用 AgentManager/WebSocketChannel/Desktop 分支。

判断一个后端 Plugin 是否被 F1 优雅支持，使用以下标准：

1. 只依赖公开 Service 和协议类型。
2. 不导入 `runtime/`、`providers/` 或其他能力的私有模块。
3. 不修改 Composition Root 才能注册普通贡献；只有新增 required Provider 才允许修改默认 Composition。
4. 装载、失败、重载和卸载均能通过 Fiber/Effect 解释。
5. 不需要模块全局 setter、单例或隐式 import 副作用。

## 2. 需求范围

### 2.1 功能需求

- [ ] **FR1：引入唯一 Cordis 基座。** `pyproject.toml` 必须声明经评审锁定的 `cordis-py 0.4.x` 依赖；生产代码使用 `cordis.Context/Fiber/Service/Inject/Effect`，不再维护功能重复的 FtreContext、EventHub、PluginRegistry 和自研生命周期状态机。
- [ ] **FR2：建立四层目录。** `src/ftre` 下的主要实现必须归入 `app`、`platform`、`services`、`features`；每个迁移文件只有一个能力 Owner，不创建空目录、占位函数或为未来设想准备的无实现模块。
- [ ] **FR3：建立唯一 Composition Root。** `app/gateway/bootstrap.py` 创建根 Context；`composition.py` 明确声明默认 Provider Plugin 和行为 Plugin 的装载清单；`main.py` 只保留 CLI 暴露与参数转发。
- [ ] **FR4：定义稳定 Service。** Config、Filesystem、Workspace、Http、MessageBus、Session、SystemPrompt、AgentProfile、Agent、Tool、Command、Channel、Attachment、Trace、MCP、Skill、Schedule 和 Team 的共享能力通过有明确 key 的 Service 暴露；Consumer 不直接导入其他能力的默认 Provider 私有实现。
- [ ] **FR5：注册行为必须受生命周期管理。** Tool、Command、Cordis Event/Hook、Prompt Contribution、Channel、后台任务和资源连接必须通过 Fiber/Effect 注册并在卸载时撤销；Router Contribution 在 HTTP App 冻结前可撤销，冻结后的变更返回 `restart_required`。
- [ ] **FR6：分离发现、启用与运行状态。** Catalog 记录候选插件；配置和默认 Composition 决定启用项；Manager 记录对应 Fiber。扫描到但未启用的外部插件不得执行导入后的业务初始化。
- [ ] **FR7：插件依赖响应式生效。** Consumer 使用 `Inject` 或等价 Cordis 注入声明强依赖；依赖缺失时 Fiber 保持 `PENDING`，Provider 上线后自动激活，Provider 下线或替换时 Consumer 自动卸载或重载。
- [ ] **FR8：配置单一所有权。** `ConfigService` 是 `~/.ftre/config.json` 的唯一读写和监听入口；提供 typed snapshot、单调 revision、expected-revision 原子更新和 watcher；移除模块级 `DEFAULT_CONFIG` 事实源；MCP、Skill、Agent Profile 和 HTTP API 不得各自直接覆盖根配置文件。
- [ ] **FR9：拆分 HTTP Service 与 Server。** `services/http/service.py` 提供 exact/prefix/APIRouter Contribution、owner、body limit、same-origin helper、freeze 与 `restart_required`；Gateway 的 Http Service Provider 创建 FastAPI App；Server Plugin 必须在所有启动期 Route 就绪后启动 uvicorn，并在卸载时停止。
- [ ] **FR10：保持并封装 Agent Runtime。** `AgentService` 提供 submit/cancel/wait/status/list/get/is_busy、Agent created/disposed 事件和 agent Tool Scope；Agent Factory、AgentLoop、SessionLane、Mailbox、ContextGate、CompletionRegistry 和 Compaction 下沉到默认 Provider 的 `runtime`，Consumer 不访问其私有字段。
- [ ] **FR11：保持双事件平面。** EventBus 的消息、ACK、request/reply 和 admission 语义保持不变；Plugin Hook 迁移到 Cordis Event，不得用 Cordis Event 替代业务 MessageBus。
- [ ] **FR12：按能力迁移现有功能。** Skill、MCP、Plan、Team、Schedule、ContextGovern 和 SessionTitle 回到各自功能包；Plugin 入口只负责装配，Store、Service、Tool、Router 和内部实现拆为相邻模块。
- [ ] **FR13：显式装载外部插件。** `~/.ftre/plugins` 只作为候选来源；外部插件必须出现在配置启用项中才可装载。规范入口使用 `module:attribute`，F1 迁移期兼容已有 `module.Class` 写法。
- [ ] **FR14：保持 Octo 可用。** Octo 保持独立仓库和现有业务逻辑；其入口迁移为 Cordis Plugin，或在评审明确接受的短期兼容层上运行。最终不得继续依赖已删除的自研 Kernel。
- [ ] **FR15：提供启动诊断。** Plugin Manager 至少输出 id、source、entry、FiberState、required、error 和 `restart_required`；必需插件失败必须阻止 Gateway 对外监听，不能只记录 warning 后继续。
- [ ] **FR16：迁移过程持续可运行。** 每个子阶段结束后必须保持测试、lint 和 Gateway 启动基线通过；禁止一次性移动全部文件后再统一修复导入。
- [ ] **FR17：提供可审计的 scoped registry。** ToolService 必须提供 global/agent scope、allow/deny restriction、当前 Agent 实际可见 schema 及 owner/source；SkillService 必须提供来源、优先级、winner/shadow；SystemPromptService 必须产生最终 Assembly Receipt。上述信息必须来自实际装配结果，不由诊断 Plugin 重新猜测。
- [ ] **FR18：统一 Filesystem 与 Workspace 边界。** FilesystemService 提供 resolve/stat/read/atomic-write 和路径策略，WorkspaceService 提供 session workspace 读取与切换；Tool 和 Plugin 不重复实现路径归一化、工作区逃逸防护与同步桥接。
- [ ] **FR19：用公开契约验证架构。** 建立 synthetic architecture fixture，模拟只读审计型第三方 Plugin；它只能依赖公开 Service，必须能读取当前 Agent 的 Tool/Skill/Prompt 可见面并完成生命周期清理。该 fixture 不复制或迁移任何外部仓库代码。

### 2.2 非功能需求

- **兼容性**：Python 3.12；现有 `config.json`、Session JSON 数据、Agent 配置合并、HTTP/WS 路径与消息载荷保持兼容。
- **生命周期**：根 Context dispose 后不得遗留事件监听、Tool/Command 注册、Channel、MCP 连接、Cron Task、uvicorn Task 或文件 watcher。
- **可维护性**：`app → features → services → platform` 为主依赖方向；`services` 不得导入 `features` 或 `app`；`platform` 不得导入产品能力。
- **扩展性**：新增普通后端 Plugin 不得修改 AgentManager、AgentLoop、WebSocketChannel、FastAPI App 构造器或现有 Feature 实现；只允许通过 Manifest、Composition 配置和公开 Service 装配。
- **可审计性**：注册型 Service 必须保留 owner/source/scope，并能返回当前有效视图；不得只保留无来源的最终 dict/list。
- **启动可靠性**：必需 Provider 缺失或配置非法时 fail loud；HTTP Server 不得在 Composition 未完成前接流量。
- **安全性**：发现外部目录时不得执行未启用插件的 setup；配置中的入口必须限制在明确候选目录或显式受信包。
- **性能**：F1 不引入额外常驻进程；MessageBus 与 AgentLoop 主路径不得增加轮询等待或跨进程序列化。
- **可观测性**：插件装载失败必须保留插件 id、入口、依赖状态和异常摘要；日志使用 Python `logging`。
- **代码质量**：禁止空函数、占位实现和遗留调试输出；公共 Service 与 Plugin 入口必须有类型标注；开发依赖必须包含项目验收所需的 pytest、pytest-asyncio、httpx 和 ruff。

## 3. 技术方案

### 3.1 目标逻辑目录

以下是 F1 的逻辑目标。目录只在迁移真实代码时创建：

```text
src/ftre/
├─ main.py
│
├─ app/
│  ├─ README.md
│  ├─ cli/
│  │  ├─ app.py
│  │  ├─ logging.py
│  │  └─ gateway_process.py
│  └─ gateway/
│     ├─ bootstrap.py
│     ├─ composition.py
│     ├─ diagnostics.py
│     └─ http/
│        ├─ service_plugin.py
│        ├─ app.py
│        ├─ server.py
│        ├─ server_plugin.py
│        └─ health.py
│
├─ platform/
│  ├─ README.md
│  └─ plugin_runtime/
│     ├─ manifest.py
│     ├─ catalog.py
│     ├─ discovery.py
│     ├─ loader.py
│     ├─ manager.py
│     └─ diagnostics.py
│
├─ services/
│  ├─ README.md
│  ├─ config/
│  │  ├─ service.py
│  │  ├─ plugin.py
│  │  ├─ models.py
│  │  ├─ paths.py
│  │  ├─ store.py
│  │  └─ router.py
│  ├─ filesystem/
│  │  ├─ service.py
│  │  ├─ plugin.py
│  │  ├─ target.py
│  │  ├─ policy.py
│  │  └─ local.py
│  ├─ workspace/
│  │  ├─ service.py
│  │  └─ plugin.py
│  ├─ http/
│  │  ├─ service.py
│  │  ├─ types.py
│  │  └─ security.py
│  ├─ system_prompt/
│  │  ├─ service.py
│  │  ├─ plugin.py
│  │  ├─ types.py
│  │  ├─ receipt.py
│  │  └─ base.md
│  ├─ messaging/
│  │  ├─ bus/
│  │  │  ├─ service.py
│  │  │  ├─ plugin.py
│  │  │  ├─ message.py
│  │  │  ├─ payloads.py
│  │  │  └─ protocol.py
│  │  └─ channel/
│  │     ├─ service.py
│  │     ├─ plugin.py
│  │     ├─ base.py
│  │     └─ providers/
│  │        ├─ websocket/
│  │        │  ├─ plugin.py
│  │        │  ├─ channel.py
│  │        │  └─ protocol.py
│  │        └─ subagent/
│  │           ├─ plugin.py
│  │           └─ channel.py
│  ├─ session/
│  │  ├─ service.py
│  │  ├─ plugin.py
│  │  ├─ router.py
│  │  ├─ entity/
│  │  │  ├─ models.py
│  │  │  └─ state.py
│  │  ├─ persistence/
│  │  │  ├─ repository.py
│  │  │  └─ json_store.py
│  │  ├─ message/
│  │  │  ├─ converter.py
│  │  │  ├─ multimodal.py
│  │  │  └─ token_counter.py
│  │  ├─ projection.py
│  │  ├─ search.py
│  │  └─ title/
│  │     ├─ config.py
│  │     └─ plugin.py
│  ├─ agent/
│  │  ├─ service.py
│  │  ├─ plugin.py
│  │  ├─ events.py
│  │  ├─ router.py
│  │  ├─ profile/
│  │  │  ├─ service.py
│  │  │  ├─ plugin.py
│  │  │  └─ models.py
│  │  └─ runtime/
│  │     ├─ factory.py
│  │     ├─ loop/
│  │     │  ├─ engine.py
│  │     │  ├─ turn_executor.py
│  │     │  ├─ context_gate.py
│  │     │  └─ completion_registry.py
│  │     ├─ mailbox/
│  │     │  ├─ lane.py
│  │     │  └─ store.py
│  │     └─ compaction/
│  │        ├─ manager.py
│  │        └─ events.py
│  ├─ tools/
│  │  ├─ service.py
│  │  ├─ plugin.py
│  │  ├─ types.py
│  │  ├─ scope.py
│  │  ├─ filesystem/
│  │  ├─ shell/
│  │  ├─ workspace/
│  │  └─ messaging/
│  ├─ command/
│  │  ├─ service.py
│  │  ├─ plugin.py
│  │  ├─ types.py
│  │  └─ builtin.py
│  ├─ attachment/
│  │  ├─ service.py
│  │  ├─ plugin.py
│  │  ├─ store.py
│  │  └─ router.py
│  └─ observability/
│     └─ trace/
│        ├─ service.py
│        ├─ plugin.py
│        ├─ store.py
│        └─ router.py
│
└─ features/
   ├─ README.md
   ├─ mcp/
   │  ├─ service.py
   │  ├─ plugin.py
   │  ├─ config.py
   │  ├─ adapter.py
   │  ├─ private.py
   │  └─ router.py
   ├─ skill/
   │  ├─ service.py
   │  ├─ plugin.py
   │  ├─ types.py
   │  ├─ store.py
   │  ├─ tool.py
   │  └─ router.py
   ├─ plan/
   │  ├─ plugin.py
   │  └─ tool.py
   ├─ team/
   │  ├─ service.py
   │  ├─ plugin.py
   │  ├─ profile.py
   │  └─ tools.py
   ├─ schedule/
   │  ├─ service.py
   │  ├─ plugin.py
   │  ├─ store.py
   │  ├─ channel.py
   │  ├─ tool.py
   │  └─ router.py
   └─ context_govern/
      └─ plugin.py
```

测试目录：

```text
tests/
├─ unit/               # 镜像 app/platform/services/features
├─ architecture/       # 导入边界和公共 API
├─ contracts/          # Service 行为契约
├─ lifecycle/          # Fiber/Effect/资源清理
├─ startup/            # 默认 Composition
└─ integration/
```

#### 3.1.1 四个导航层的所有权

| 导航层 | 可以拥有 | 禁止拥有 |
|---|---|---|
| `app` | CLI、进程参数、Composition Root、FastAPI/uvicorn Host、启动诊断 | Session/Agent/MCP 业务规则、Tool 实现、Plugin 扫描策略之外的框架内核 |
| `platform` | 对 cordis-py 的项目级 Manifest/Catalog/Loader/Manager/Diagnostics 薄适配 | Tool/Session/Agent 等产品 Service，实现自己的 Fiber/Event/DI |
| `services` | 多个 Consumer 共享的稳定运行时能力及默认 Provider | 只服务单一 Feature 的业务行为、Desktop UI、外部插件来源分类 |
| `features` | 完整产品功能；可提供 Service，也可只消费 Service 注册行为 | 直接创建全局 Host、读取其他 Feature 私有 Store、导入 Agent Runtime 私有实现 |

#### 3.1.2 Group README 最低内容

四个导航层以及 `services`/`features` 下的重要能力必须有简短 README，至少记录：

1. Owner 与一句话职责。
2. 公共导出和 Service key。
3. Provider Plugin、Consumer 和依赖方向。
4. 持久化文件/目录所有权。
5. 生命周期资源及清理方式。
6. 已知限制与后续 TODO。

README 是导航和边界说明，不复制实现文档；实际行为仍由 PRD、类型和测试约束。

### 3.2 角色约定

目录表达能力所有权，文件表达架构角色：

| 约定 | 职责 |
|---|---|
| `<capability>/service.py` | 稳定 Service API；其他能力只依赖这一层 |
| `<capability>/plugin.py` | Cordis Provider 或 Consumer Plugin 装载入口 |
| `<capability>/server.py` | uvicorn、WebSocket 等网络资源；由 Plugin 创建和回收 |
| `<capability>/store.py` | 持久化实现，不承担 Plugin 装配 |
| `<capability>/router.py` | HTTP Consumer，通过 HttpService 注册 |
| `<capability>/tool.py` | 模型可见 Consumer，通过 ToolService 注册 |
| `<capability>/runtime/*` | 默认 Provider 私有实现，不能作为跨能力导入面 |

并非每个能力机械创建所有文件：

- 只有一个实现且公共 API 很小，可以由 `service.py` 同时承载 Service 类与默认实现。
- `context_govern`、`plan`、`session/title` 等纯行为 Plugin 不创建空 Service。
- `factory.py` 永远是私有构造辅助，不能代替 `service.py` 或 `plugin.py`。

### 3.3 分层和依赖规则

主依赖方向：

```text
app ───────→ features ───────→ services
 │               │                │
 └───────────────┴──────→ platform
                 │                │
                 └──────→ cordis-py
```

约束：

1. `platform` 不导入 `services/features/app`。
2. `services` 不导入 `features/app`。
3. `features` 可以依赖多个公共 Service，但不能导入其 `runtime` 或具体 Provider。
4. `app` 只做最终装配、进程入口和 Host 资源管理，不承载 Session、Agent、MCP 等业务规则。
5. 同层能力间必须通过 Service、事件或消息协议协作，不得访问对方私有字段。
6. `ftre-agent-core` 仍是无状态算法库，只由 Agent 默认 Provider 调用。

### 3.4 Service 清单

Service key 在 F1 内视为稳定接口，禁止用 Python 类名或目录名隐式推导：

| Service key | Owner | 主要职责 | 典型 Consumer |
|---|---|---|---|
| `config` | `services/config` | 根配置读写、typed snapshot、watch | MCP、Skill、Agent、HTTP |
| `filesystem` | `services/filesystem` | 路径解析、stat/read/atomic-write、边界策略 | Tool、Skill、ContextGovern、Plugin |
| `workspaces` | `services/workspace` | Session workspace 查询、切换和扩展目录 | Agent、Tool、HTTP、Skill、MCP |
| `http` | `services/http` | Router Contribution Registry、冻结状态 | 各 `router.py`、Gateway Server |
| `system_prompt` | `services/system_prompt` | 有序 Prompt Section 注册、按 Agent/Session 组装 | Agent Factory、Skill、MCP、ContextGovern |
| `message_bus` | `services/messaging/bus` | Channel ↔ Agent 业务消息、ACK | Channel Provider、AgentService |
| `channels` | `services/messaging/channel` | Channel 注册、启停和发送 | Gateway、Schedule、Team |
| `sessions` | `services/session` | Session CRUD、事件追加、搜索 | Agent、HTTP、Team |
| `agent_profiles` | `services/agent/profile` | Agent 配置与 Prompt/Profile 合并 | Agent Factory、HTTP、Team |
| `agents` | `services/agent` | submit、cancel、wait、status | Channel、Command、HTTP、Team |
| `tools` | `services/tools` | Tool 注册、执行和 agent-scoped view | Agent Factory、功能 Plugin |
| `commands` | `services/command` | Command 注册和 dispatch | Channel、Skill、Agent |
| `attachments` | `services/attachment` | 附件校验、存储和解析 | HTTP、WebSocket、Tool |
| `traces` | `services/observability/trace` | Trace 写入和查询 | Agent、HTTP |
| `mcp` | `features/mcp` | MCP 连接池、全局/私有连接状态 | MCP Router、Agent |
| `skills` | `features/skill` | Skill Catalog、加载和配置 | Skill Tool、HTTP、Prompt |
| `schedule` | `features/schedule` | Cron 持久化、触发和状态 | Cron Tool、HTTP、Channel |
| `teams` | `features/team` | Team/Profile/成员生命周期 | Team Tool、Session 清理协调 |

Agent 对外接口只暴露 `agents`；`AgentLoop` 不再作为任意模块可直接访问的全局对象。

#### 3.4.1 所有 Service 的共同规则

1. Service key 使用上表固定字符串，通过 Cordis `Service.provide` 或等价声明注册。
2. Provider 的 `plugin.py` 负责创建 Service；Service 不在模块 import 时自动实例化。
3. Consumer 通过 `Inject` 和 `ctx.<service_key>` 使用 Service，不导入 Provider 类。
4. 所有 `register/add/watch/on` 方法必须返回幂等 disposer；Provider Plugin 将 disposer 绑定到当前 Fiber。
5. 注册记录至少保存 `owner`、`source` 和 `scope`，诊断面不得只返回无来源的最终值。
6. 同一 scope 下的稳定标识冲突默认 fail loud，不静默覆盖；确需 shadow 的能力必须在自己的契约中明确优先级。
7. 公开查询方法返回不可变 snapshot 或副本，Consumer 不能修改 Service 内部容器。
8. Service 自身拥有状态与锁；调用方不能通过读取 `_items/_lanes/_registry` 等私有字段绕过约束。
9. Provider 下线时，依赖它的 Consumer Fiber 由 Cordis 进入 PENDING/卸载流程；不由 Consumer 捕获 `AttributeError` 后继续假装可用。

公共注册元数据：

| 字段 | 含义 |
|---|---|
| `owner` | 注册该贡献的 Plugin id，例如 `skill`、`mcp` |
| `source` | `builtin`、`external:<id>` 或 `system` |
| `scope` | `global` 或 `agent:<agent_id>`；需要时可增加 `session:<session_id>`，F1 不开放任意自定义 scope |
| `registered_at` | 进程内单调序号，用于稳定排序和诊断，不作为持久化时间 |

#### 3.4.2 ConfigService

ConfigService 必须提供以下行为：

| 方法/属性 | 行为 |
|---|---|
| `snapshot()` | 返回 `{revision, value}` typed snapshot；value 不可由调用方原地修改 |
| `update(patch, expected_revision)` | 校验完整候选配置；revision 不一致抛冲突；成功后原子写盘并递增 revision |
| `replace(value, expected_revision)` | 仅供完整配置 API 和迁移工具使用；同样执行 schema 校验和原子写 |
| `watch(callback)` | 配置成功提交后通知；返回 disposer；同一 revision 只通知一次 |
| `plugin_config(plugin_id)` | 返回 Plugin 的有效配置副本，不暴露整个可变根 dict |
| `path` | 只读暴露当前实际配置文件绝对路径 |

写入约束：

1. 在目标目录创建临时文件，写入并 flush 后使用 `os.replace` 原子发布。
2. 新配置校验失败、revision 冲突或写盘失败时，内存 active snapshot 不改变。
3. 保留当前 schema 未识别但合法的顶层字段，避免 Desktop 或旧版本字段在 round-trip 中丢失。
4. watcher 识别自身写入，不能因同一次 update 触发重复 reload。
5. F1 保持现有明文 provider key 行为兼容，但任何诊断和日志不得输出 secret 值。

#### 3.4.3 FilesystemService 与 WorkspaceService

FilesystemService 的最小接口：

| 方法 | 行为 |
|---|---|
| `resolve(path, cwd, policy)` | 返回规范化 Target；支持绝对/相对路径，执行 root/allowed_dirs 边界检查 |
| `stat(target)` | 返回 file/directory、size、mtime 等只读元数据；不存在返回明确 NotFound |
| `read_text(target, limit, encoding)` | 限制读取字节数；编码回退策略集中管理 |
| `read_bytes(target, limit)` | 受限二进制读取，供 Attachment/图片能力使用 |
| `write_text_atomic(target, content)` | 原子写，不允许越过 policy |
| `mkdir(target, parents)` | 只在调用方明确请求时创建目录；resolve 本身无副作用 |

WorkspaceService 的最小接口：

| 方法 | 行为 |
|---|---|
| `get(session_id)` | 返回 Session 持久化 workspace；无效路径按现有 fallback 规则处理 |
| `set(session_id, absolute_path)` | 校验存在且为目录，写入 Session，返回变更前后值 |
| `policy(session_id, allowed_dirs=())` | 生成供 Filesystem/Tool 使用的 PathPolicy |
| `ensure_extension_layout(session_id)` | 幂等创建现有 `.ftre/skills`、`.ftre/mcp.json` 骨架；失败记录日志但不伪造成功 |

现有 `WorkspaceAccessor` 可以作为 Tool 执行线程的兼容适配器，但只调用 WorkspaceService，不再直接持有 SessionManager。

#### 3.4.4 HttpService

HttpService 是 Route Contribution Registry，不是 uvicorn Server：

| 方法/属性 | 行为 |
|---|---|
| `register_router(router, owner, prefix='/api')` | 注册 FastAPI APIRouter；返回 disposer |
| `register_route(method, path, handler, owner, kind='exact')` | 注册 exact/prefix 原始路由；用于文件流或非 APIRouter 场景 |
| `snapshot()` | 返回 owner、method/kind、path、冻结状态 |
| `freeze()` | 固化本次 FastAPI App；之后新增/移除只更新 registry 并设置 restart_required |
| `restart_required` | 表示运行中 App 与 registry 已不一致 |
| `same_origin(request)` | 统一 Host 同源判断；状态变更 Route 不自行发明规则 |

冲突规则：

- 同一 HTTP method + exact path 只能有一个 owner。
- prefix route 不能与另一个相同 prefix 重复；更具体 exact route 由 Host 按确定顺序优先。
- `/api` 既有路径保持不变；WebSocket 仍使用 `/`。
- freeze 前 disposer 必须从构建清单移除 Route；freeze 后 disposer 设置 restart_required。

#### 3.4.5 ToolService

ToolService 包装 `ftre-agent-core` 的 ToolRegistry，不把进程级可变注册表下沉到 core：

| 方法 | 行为 |
|---|---|
| `register(tool, owner, scope='global')` | 注册 Tool，返回 disposer；同 scope 同名冲突失败 |
| `restrict(agent_id, owner, allow=None, deny=None)` | 注册 Agent 视图限制，返回 disposer |
| `snapshot(agent_id=None)` | 返回当前 scope 合并后的 ToolDefinition snapshot |
| `schemas(agent_id=None)` | 返回模型实际看到的 schema，并包含 owner/source/scope 元数据 |
| `build_view(agent_id, session_id)` | 生成 Agent 本轮 ToolRegistryView；不复制后再由调用方任意修改 |
| `execute(name, execution_context, arguments)` | 统一注入 session/workspace/cancellation/metadata，保持现有返回值兼容 |

Scope 合并规则：

1. global Tool 是基础集合。
2. agent scope Tool 可以为该 Agent 新增同名覆盖，但必须在诊断中显示 shadow 关系。
3. deny 最后生效；allow 非空时只保留 allow 集合。
4. Tool Schema 查询必须与 Agent 本轮实际 view 相同，不能返回未暴露 Tool。
5. Plugin 卸载后，只移除自己贡献的 Tool/restriction；之前被 shadow 的 Tool 自动恢复。

#### 3.4.6 SkillService

SkillService 统一当前全局、Agent 和 Workspace 三层 Skill：

| 方法 | 行为 |
|---|---|
| `list(agent_id, workspace)` | 返回 resolved catalog 与 shadow 信息 |
| `get(name, agent_id, workspace)` | 返回胜出 Skill 完整内容 |
| `register(skill, owner, scope)` | 注册 Runtime/Builtin Skill，返回 disposer |
| `sources(name, agent_id, workspace)` | 返回全部候选、优先级、winner 和 shadowed |
| `mark_loaded(session_id, name, source)` | 记录本 Session 已成功加载的 Skill evidence |

优先级保持现有产品语义：

```text
workspace > agent > global > bundled/runtime
```

同一优先级同名候选不按遍历顺序碰运气，必须报告冲突并使用稳定的 owner/id 排序作为临时决胜规则。

#### 3.4.7 SystemPromptService

SystemPromptService 不允许 Plugin 直接修改 `config.system_prompt` 或第一条 system message：

| 方法 | 行为 |
|---|---|
| `register_section(section, owner, scope='global')` | 注册有名 Prompt Section，返回 disposer |
| `assemble(agent_id, session_id, workspace, messages)` | 按稳定顺序构建本轮 system prompt |
| `receipt(agent_id, session_id, workspace, messages)` | 返回与同一 assemble 输入对应的真实 Assembly Receipt |

Prompt Section 至少包含：

| 字段 | 说明 |
|---|---|
| `name` | owner 内稳定名称 |
| `content` 或 `factory` | 静态内容或按运行上下文生成内容 |
| `priority` | 数值越小越靠前；同值按注册序号 |
| `scope` | global/agent/session |
| `required` | 构造失败是否阻止本轮 Agent 执行 |

Assembly Receipt 至少记录 `name/owner/source/scope/order/bytes/token_estimate/included/error`。F1 不实现自动 Prompt 裁剪，但 receipt 必须如实标记所有实际包含项。

#### 3.4.8 AgentProfileService 与 AgentService

AgentProfileService 保持现有合并语义：

- `llm`：provider/model 可覆盖；api_key/base_url/vision 使用全局。
- `tools`、`disabled_skills`：Agent 写了则整体替换。
- `mcp`、`plugins`：按现有 server/name 规则合并。
- workspace 为 Agent 默认值，Session 已持久化 workspace 优先。

最小接口：

| Service | 方法 | 行为 |
|---|---|---|
| AgentProfileService | `list/get/create/update/delete` | 管理 `~/.ftre/agents/<id>` 与 typed profile |
| AgentProfileService | `resolve(agent_id, session_id)` | 返回合并完成且不可变的 EffectiveProfile |
| AgentService | `submit` | 接收已通过 MessageBus admission 的工作 |
| AgentService | `cancel` | 按 session/request_id 精确取消 |
| AgentService | `wait` | 按 request_id 使用 CompletionRegistry 等待 |
| AgentService | `status/is_busy` | 返回公开运行状态，不暴露 Lane |
| AgentService | `list/get` | 返回当前活动 Agent Handle 的只读视图 |
| AgentService | `on_created/on_disposed` | 生命周期订阅，返回 disposer |
| AgentService | `tool_scope(agent_id)` | 返回该 Agent 的 Tool scope handle |

AgentLoop 是 AgentService 默认 Provider 的内部引擎。HTTP、Command、Team、Channel 和 Plugin 不再读取 `agent_loop._lanes`、`session_projection` 或其他私有成员。

#### 3.4.9 Session、MessageBus 与 Channel

SessionService 继续是 Session 业务唯一入口，Repository/JsonStore 只供 Provider 使用。它至少提供现有 CRUD、message/event append、fork、search、token usage 和 workspace 字段操作。

MessageBus 保持现有：

- inbound/outbound queue。
- `request_inbound` admission ACK。
- request_id 幂等与 request/reply。
- Channel → AgentService 数据面。

ChannelService：

| 方法 | 行为 |
|---|---|
| `register(channel, owner)` | channel_id 冲突失败；返回 async-aware disposer |
| `start_all/stop_all` | 只由 Provider/Composition 调用 |
| `send(channel_id, message)` | 通过公开协议发送 |
| `snapshot()` | 返回 channel_id、owner、state |

Cordis Event 不能承载上述业务消息；MessageBus 也不能代替 Plugin lifecycle event。

#### 3.4.10 Attachment、Trace 与 Feature Service

- AttachmentService 负责附件 identity、大小/MIME 校验、存储与读取；HTTP 只负责传输。`image_store.py` 的裸路径能力迁入后必须受 Workspace/Filesystem policy 约束。
- TraceService 负责 trace/run 写入、查询和 retention；Router 不直接读取 SQLite 全局路径。
- McpService 负责连接池和 server 状态；MCP Tool 通过 ToolService 注册。
- ScheduleService 负责 Job Store、触发和状态；Cron Channel、Tool、Router 都消费它。
- TeamService 负责 Team/Profile/成员级联；SessionService 不导入 Team 实现。
- Plan 和 ContextGovern 若没有共享状态，保持纯行为 Plugin，不创建空 Service。

### 3.5 Plugin Manifest、Catalog 与配置

ftre 不重新定义 Cordis Plugin 基类。合法 Plugin 形态直接沿用 `cordis-py` 支持的函数、类或对象；ftre Manifest 只保存项目级装载元数据：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `str` | 稳定唯一 id |
| `entry` | `str/callable` | 规范入口 `module:attribute` 或已解析 callable |
| `source` | `builtin/external` | 候选来源 |
| `required` | `bool` | 失败是否阻止 Gateway 启动 |
| `default_enabled` | `bool` | Builtin 是否进入默认 Composition；External 固定为 false |
| `version` | `str/None` | 仅用于诊断和兼容提示，不参与 Python 包解析 |
| `description` | `str` | 简短人类可读说明 |
| `config` | `dict` | 传给 `ctx.plugin(entry, config)` 的配置 |

Manifest 不是新的 Plugin 基类，也不声明 Cordis 依赖。Plugin 依赖以 `Inject` 为唯一运行时事实源，避免 Manifest 与代码出现两套依赖列表。

#### 3.5.1 Plugin 类型

| 类型 | 定义 | 示例 | 是否提供 Service |
|---|---|---|---|
| Provider Plugin | 创建一个有状态 Service，并用 Effect 管理其资源 | Config、Session、Agent、MCP | 通常是 |
| Behavior Plugin | 消费 Service，注册 Tool/Prompt/Hook/Router 等行为 | ContextGovern、Plan、SessionTitle | 可以不提供 |
| Adapter Plugin | 将外部协议或实现接到稳定 Service | WebSocket Channel、Subagent Channel、JSON Store | 可选 |
| Host Plugin | 创建进程级资源，必须在 Composition 指定阶段装载 | HttpService Provider、uvicorn Server | 通常提供或消费 Host Service |

`builtin/external` 是来源，Provider/Behavior/Adapter/Host 是角色，两者不能混为一谈。例如 Octo 是 external Adapter/Behavior Plugin；WebSocket 是 builtin Adapter Plugin。

#### 3.5.2 Plugin id 与入口约束

1. id 必须匹配 `[a-z][a-z0-9_-]{1,63}`，作为配置、诊断、owner 和日志的稳定标识。
2. Builtin id 由 `composition.py` 和 Catalog 常量声明；代码移动时 id 不变。
3. External canonical entry 为 `module:attribute`；attribute 可以是 Cordis 支持的函数、类或带 `apply` 的对象。
4. External 路径必须位于配置的插件根目录或显式受信任 Python 包；解析后对 realpath 做边界校验。
5. 未启用 External 不 import；不能通过 import 来读取 name/version。
6. module import 失败、attribute 不存在、Plugin 形态非法和配置校验失败分别保留不同 error code。
7. 同一进程中一个 id 只对应一个启用实例；Cordis 支持的同 Plugin 多实例能力留给未来 Preset，不在 F1 配置面开放。

#### 3.5.3 默认 Composition

默认装载顺序按依赖阶段声明，而不是依赖 Python 目录遍历顺序：

| 阶段 | 默认 Plugin | required |
|---|---|---|
| 1. foundation | config、filesystem、http-service、system-prompt、message-bus | 是 |
| 2. core registries | tools、commands、channels、attachments、traces | 是 |
| 3. state/runtime | sessions、workspaces、agent-profiles、agents | 是 |
| 4. adapters | websocket-channel、subagent-channel | 是 |
| 5. features | skill、mcp、plan、team、schedule、context-govern、session-title | 按下表 |
| 6. external | 配置显式启用的外部 Plugin（含 Octo） | 由配置/Manifest 决定 |
| 7. host listen | http-server | 是，且必须最后 |

Builtin feature 默认策略：

| Plugin | 默认 | required | 失败行为 |
|---|---|---|---|
| skill | enabled | 否 | Skill 不可用，Gateway 继续并报告 FAILED |
| mcp | enabled | 否 | MCP 不可用，Gateway 继续；不得阻塞基础 Agent |
| plan | enabled | 否 | Plan Tool/Hook 不注册 |
| team | enabled | 否 | 多 Agent Team 能力不可用 |
| schedule | enabled | 否 | Cron 不启动，Job Store 不被破坏 |
| context-govern | enabled | 是 | 指令治理缺失会改变 Agent 安全/行为，阻止监听 |
| session-title | enabled | 否 | Session 仍可用，只不自动生成标题 |

required 的最终取值优先级：系统强制 required > Manifest required > 配置只允许 optional 提升为 required，不允许把系统 required 降级。

`~/.ftre/config.json` 继续使用 `plugins` 数组。迁移期示例：

```json
{
  "plugins": [
    {
      "name": "skill",
      "disabled": false,
      "config": {}
    },
    {
      "name": "octo_channel",
      "module": "octo_channel:plugin",
      "disabled": false,
      "config": {}
    }
  ]
}
```

规则：

1. Builtin 默认项由 `composition.py` 显式列出，配置只覆盖 enabled/config。
2. External 必须有配置条目且 `disabled != true` 才可装载。
3. External 发现阶段只读取配置和文件系统元数据，不 import 未启用模块，不调用 Plugin setup，不创建网络连接和后台任务；模块导入发生在启用决策之后。
4. 同一 `id` 冲突时启动失败并报告两个来源，不静默覆盖。
5. 入口不存在：required Plugin 启动失败；optional Plugin 标记 FAILED 并进入诊断。
6. F1 兼容现有 `module.Class` 入口，但日志给出迁移提示；新配置只写 `module:attribute`。
7. `group/children/isolate` 仅保留现有配置解析兼容；per-agent Preset 组合和完整 Isolate 治理不在 F1 扩展。

#### 3.5.4 配置合并规则

Builtin 有默认 Manifest 和默认 config；用户配置按 id/name 覆盖：

```text
Manifest default config
        ↓ shallow/deep merge（由该 Plugin config model 明确）
用户 config.json plugins[i].config
        ↓ schema validation
传入 ctx.plugin(entry, effective_config)
```

- 未出现在配置中的 Builtin 使用默认 enabled/config。
- `disabled: true` 禁用 optional Builtin；禁用系统 required Plugin 时配置校验失败。
- `enabled: false` 作为旧/新格式兼容别名，统一规范化为 disabled。
- 同一 id 在配置树出现多次属于错误，不使用“最后一个覆盖”。
- group disabled 时不 import children；group 只创建作用域，不是运行 Plugin。
- Plugin config 只传给自身，禁止 Plugin 读取整个根 dict 寻找私有配置。

### 3.6 生命周期与启动顺序

```text
读取最小启动参数
    ↓
创建根 cordis.Context
    ↓
加载 Config Provider
    ↓
加载 HttpService、MessageBus 等基础 Provider
    ↓
加载 Session、Agent、Tool、Command、Channel 等核心 Provider
    ↓
加载 Skill、MCP、Plan、Team、Schedule、ContextGovern 等功能 Plugin
    ↓
按配置加载显式启用的 External Plugin
    ↓
等待 required Fiber ACTIVE，并完成 Router/Tool/Hook Contribution
    ↓
冻结 HttpService Router Registry
    ↓
最后加载 HTTP Server Plugin，对外监听
```

关闭时只从根 Context 开始 dispose。各 Fiber 必须通过 Effect 清理自己的资源，不在 `main.py` 重新维护业务对象关闭清单。

Fiber 状态直接采用 `cordis.FiberState`：

`PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED`，失败进入 `FAILED`。

#### 3.6.1 状态解释

| 状态 | ftre 语义 | 是否可对外声称可用 |
|---|---|---|
| PENDING | Plugin 已登记，但 Inject 强依赖尚未满足 | 否；诊断必须列出 missing services |
| LOADING | Plugin apply/setup 正在执行，异步 Effect 尚未完成 | 否 |
| ACTIVE | apply 完成，所有 required setup Effect 已建立 | 是 |
| FAILED | import、config、apply 或异步 setup 失败 | 否；保留异常摘要与 cause chain |
| UNLOADING | 正按 LIFO 执行 cleanup | 否 |
| DISPOSED | 所有可执行 cleanup 已完成或已记录失败 | 否 |

Plugin Manager 不复制状态机，只把 Manifest 与 Cordis Fiber 状态关联。`restart` 等价于对现有 Fiber 执行 Cordis restart，并重新读取该 Plugin 的有效 config。

#### 3.6.2 启动失败矩阵

| 失败场景 | required | optional |
|---|---|---|
| 根配置解析/校验失败 | 不创建正常 Composition，不监听端口 | 不适用 |
| Plugin id 冲突 | 整体启动失败 | 整体启动失败；冲突不能按 optional 忽略 |
| External entry import 失败 | 根 Context dispose，HTTP 不监听 | 标记 FAILED，其他 Plugin 可继续 |
| Inject 依赖缺失 | 等待到 Composition settle；仍 PENDING 则启动失败 | 保持 PENDING，并出现在诊断 |
| Plugin apply/setup 抛错 | 根 Context dispose，HTTP 不监听 | 标记 FAILED，清理已建立 Effect 后继续 |
| Router 冲突 | 若涉及 required owner 则启动失败 | 冲突 owner 标记 FAILED，不覆盖原 Route |
| cleanup 抛错 | 聚合错误、继续清理其他 Effect，最终启动命令非零退出 | 同左，不因一个 cleanup 中断全树清理 |

启动日志至少包含：Composition 阶段、Plugin id、source、state、耗时、missing dependency 或 error code。日志不得包含 config secret。

#### 3.6.3 运行期管理约束

1. required Provider 在 HTTP Server 运行时不允许被普通 `unload`；必须走协调的 Gateway restart。
2. optional Behavior Plugin 可 unload/restart；其 Tool/Command/Hook/Prompt/Channel 贡献必须立即撤销。
3. 涉及已冻结 Router 的 Plugin unload/restart 设置 `restart_required`，当前 FastAPI App 不声称热更新成功。
4. Plugin restart 期间依赖它的 Consumer 按 Cordis 语义进入 PENDING，再随 Provider ACTIVE 自动恢复。
5. Manager 不负责安装、升级或删除磁盘上的 Plugin 包；F1 的管理范围仅为已配置候选的状态与生命周期。

#### 3.6.4 关闭顺序

关闭入口只有根 Context dispose，但资源依赖仍必须满足以下效果：

```text
停止接收新 HTTP/WebSocket admission
    ↓
停止 Schedule/Watcher 和新后台任务
    ↓
取消或等待 Agent active turn（保持现有安全语义）
    ↓
关闭 External/Feature Plugin
    ↓
关闭 Channel、MCP、Tool/Command contribution
    ↓
关闭 Session/Trace/Config 等持久化 Service
    ↓
完成根 Context dispose
```

实现不得在 `main.py` 重复这一列表；顺序来自 Fiber 父子关系、Inject 依赖和 Effect LIFO。startup/lifecycle 测试验证最终效果。

### 3.7 HTTP Host

HTTP 相关角色必须分开：

| 文件 | 角色 |
|---|---|
| `services/http/service.py` | HttpService：接收 Router Contribution，维护冻结和 restart_required 状态 |
| `app/gateway/http/service_plugin.py` | 创建 HttpService 与 FastAPI App |
| `app/gateway/http/app.py` | 构造 FastAPI、CORS 和异常处理表面 |
| `app/gateway/http/server.py` | uvicorn Server 资源 |
| `app/gateway/http/server_plugin.py` | 在 Composition 完成后启动 Server，并注册停止 Effect |

Session、Agent、Config、Schedule、MCP、Skill、Trace 和 Attachment Router 由各自 Owner 提供，不再集中在一个 `api/routes.py`。

F1 只保证启动期 Router Composition。Server 启动后发生的 Router Plugin 装卸必须标记 `restart_required=true`，重启 Gateway 后生效。

#### 3.7.1 现有路由所有权

迁移后 URL、method、status code 和主要 JSON 形状保持不变，只改变代码 Owner：

| 现有表面 | 新 Owner |
|---|---|
| `GET /api/traces*` | `services/observability/trace/router.py` |
| `/api/sessions*`、`GET /api/workspaces` | `services/session/router.py` |
| `GET/PUT /api/config` | `services/config/router.py` |
| `/api/cron*` | `features/schedule/router.py` |
| `GET /api/health` | `app/gateway/http/health.py` |
| `GET /api/commands` | `services/command/router.py` 或 Command Provider 的 Router contribution |
| `GET /api/image-file`、`GET /api/images/{filename}` | `services/attachment/router.py` |
| `/api/agents*`、Agent Prompt 文件 API | `services/agent/router.py` 与 AgentProfileService |
| `/api/skills*` | `features/skill/router.py` |
| `/api/mcp*` | `features/mcp/router.py` |
| `WebSocket /` | WebSocket Channel Provider，通过 Host 注册 |

Router 不保存 Service 模块全局变量。允许的依赖取得方式只有：

1. Router 注册闭包捕获公共 Service。
2. FastAPI dependency 从 App State 读取公共 Service handle。

禁止继续使用 `set_session_manager/set_agent_loop/set_command_manager/set_agent_manager`。

#### 3.7.2 Host 构建顺序

1. HttpService Provider 创建空 Route Registry。
2. 各 Service/Feature Plugin 注册 Router。
3. Host 检查冲突并生成 FastAPI App。
4. 配置 CORS、错误映射和 WebSocket `/`。
5. HttpService freeze，记录最终 Route snapshot。
6. uvicorn Server Plugin 启动监听。

F1 不顺便改变 CORS 对 Desktop 的可连接行为；安全收紧若会影响现有客户端，另走协议 PRD。Host 必须把最终 CORS 配置记录到启动诊断，不允许 Feature Plugin 自行添加全局 Middleware。

### 3.8 主要模块迁移

| 当前模块 | F1 目标 |
|---|---|
| `main.py` | `app/cli` + `app/gateway/bootstrap.py` |
| `gateway/runtime.py` | `app/cli/gateway_process.py` |
| `plugin/kernel/*` | 由 `cordis-py` 替代；项目薄适配进入 `platform/plugin_runtime` |
| `config.py` | `services/config` |
| `system_prompt.md` 与 `AgentManager._compose_system_prompt` | `services/system_prompt`；Agent Factory 只消费最终组装结果 |
| `bus/*` | `services/messaging/bus` |
| `channel/base.py`、`channel/manager.py` | `services/messaging/channel` |
| `channel/ws_channel.py` | WebSocket Provider + HTTP Host + Attachment 三部分 |
| `session/*` | `services/session` |
| `agent/session_projection.py` | `services/session/projection.py` |
| `agent/agent_manager.py` | `services/agent/profile` + `services/agent/runtime/factory.py` |
| `agent/loop.py` 及 Lane/Compact 组件 | `services/agent/runtime`，算法语义不变 |
| `tools/*` | `services/tools`、`features/plan`、`features/team`、`features/schedule` |
| `command/*` | `services/command` |
| `api/routes.py` | 拆回各 Service/Feature 的 `router.py` |
| `api/skill.py` | `features/skill/store.py` |
| `mcp/*` + `plugin/builtin/mcp_plugin.py` | `features/mcp` |
| `plugin/builtin/skill_plugin.py` | `features/skill` |
| `plugin/builtin/context_govern.py` | `features/context_govern/plugin.py` |
| `plugin/builtin/title_gen.py` | `services/session/title/plugin.py` |
| `trace_store.py` | `services/observability/trace` |
| `utils/image_store.py` | `services/attachment` |

#### 3.8.1 迁移不变量

1. 每个文件移动必须同时更新导入、测试和公共 re-export；不允许先复制一份形成长期双实现。
2. 临时 compatibility import 只允许存在于当前 F1.x 子阶段，进入 F1.6 前全部删除。
3. 数据文件路径和格式不因 Python 模块路径改变；`~/.ftre` 下现有数据原地可读。
4. 每个能力先建立 Service contract test，再迁 Provider 和 Consumer。
5. 一个 Feature 的 Plugin、Tool、Router、Store 移动在同一可验证子阶段完成，不能留下跨新旧目录的永久循环依赖。
6. 纯路径移动不顺便改变算法；行为调整必须能够指向具体 FR/AC。
7. 不允许在 `__init__.py` 创建 Service、启动 Task 或读取配置；`__init__.py` 只做公共 re-export。

#### 3.8.2 Plugin 使用方式变化

旧方式依赖 ftre 自定义代理和隐式清理：

```python
async def setup(self, ctx, config):
    ctx.tool_registry.register(tool)
    ctx.register_router(router)
    ctx.on(BEFORE_AGENT_RUN, hook)
```

F1 方式使用 Cordis Inject + Effect：

```python
from cordis import Context, Inject


@Inject("tools")
@Inject("http")
def plugin(ctx: Context, config: dict):
    ctx.effect(
        lambda: ctx.tools.register(tool, owner="example", scope="global"),
        "example:tool",
    )
    ctx.effect(
        lambda: ctx.http.register_router(router, owner="example"),
        "example:router",
    )
    ctx.on("agent/before-run", hook)
```

差异：

- Plugin 只看到声明注入的 Service。
- 每项注册有 owner/scope 和独立 cleanup。
- 缺少依赖时 Fiber PENDING，不在 setup 内使用 None 判断。
- 卸载由 Cordis 执行，不要求 Plugin 再维护与注册列表平行的 teardown。

#### 3.8.3 Service 使用方式变化

旧 Consumer：

```python
agent_loop._lanes.cancel(session_id, request_id)
plugin_manager.tool_registry.snapshot()
CONFIG_PATH.write_text(payload)
```

F1 Consumer：

```python
await ctx.agents.cancel(session_id, request_id=request_id)
visible_tools = ctx.tools.snapshot(agent_id)
await ctx.config.update(patch, expected_revision=revision)
```

前者绑定具体对象和私有数据结构；后者绑定稳定 Service 契约，并由 Service 维护并发、校验和诊断。

#### 3.8.4 Provider 使用方式

Provider Plugin 创建 Service，Behavior Plugin 消费 Service：

```python
from cordis import Context, Service


class ExampleService(Service):
    provide = "example"

    def __init__(self, ctx: Context, config: dict):
        self.config = config
        super().__init__(ctx)


def example_provider(ctx: Context, config: dict):
    ExampleService(ctx, config)
```

Service 注册、替换和下线由 Cordis 追踪。F1 不再额外包装 `PluginInstance` 或复制 Service 状态机。

### 3.9 实施子阶段

F1 仍使用同一个 TODO id 和 PRD，但实现按以下顺序推进，每步独立保持绿：

1. **F1.1 基座**：加入 `cordis-py`；建立 `app/platform/services/features` 导航和架构测试；不改变运行行为。
2. **F1.2 Composition**：建立根 Context、Config/Http/MessageBus Provider 和唯一 bootstrap；旧入口暂由适配层调用。
3. **F1.3 核心 Service**：迁移 Session、Agent、Tool、Command、Channel；保持 AgentLoop 与协议回归。
4. **F1.4 功能 Plugin**：迁移 Skill、MCP、Plan、Team、Schedule、ContextGovern、SessionTitle。
5. **F1.5 Host 与外部插件**：拆分 FastAPI/uvicorn/WebSocket，迁移 Octo 入口，建立启动诊断。
6. **F1.6 清理**：删除旧 Kernel、全局 setter、静态 Tool Builder 和兼容导入；执行完整验收。

不得在前一子阶段测试失败时继续批量移动后续模块。

子阶段门禁：

| 子阶段 | 必须产物 | 允许的过渡 | 退出条件 |
|---|---|---|---|
| F1.1 | 目标导航目录、README、cordis 依赖、architecture test 骨架 | 旧运行入口继续工作 | 全量 pytest 保持基线；新目录无空实现；导入边界测试可运行 |
| F1.2 | Config/Filesystem/Http/SystemPrompt/MessageBus Service；根 Context 与 bootstrap | 旧 Manager 可由 Provider Adapter 包装 | `main.py` 已转发到 bootstrap；Server 仍只能在 Composition settle 后启动 |
| F1.3 | Session/Workspace/AgentProfile/Agent/Tool/Command/Channel Service | 旧类可作为新 Service 的内部实现 | 现有 AgentLoop/SessionLane/WS/Tool 回归通过；Consumer 不再读 AgentLoop 私有字段 |
| F1.4 | Skill/MCP/Plan/Team/Schedule/ContextGovern/SessionTitle 新 Plugin | 单个 Feature 迁移期间允许旧路径 re-export | 每个 Feature 的 Tool/Prompt/Router/Task 均有 lifecycle test；旧 Builtin 实现不再被加载 |
| F1.5 | Host 拆分、External 显式 Loader、Octo 兼容、Plugin Diagnostics | Router freeze 后以 restart_required 表达变化 | Gateway/Octo test-double 启停通过；未启用 External 不 import；required 失败不监听 |
| F1.6 | 删除旧目录/兼容层、文档更新、lint 债务清理 | 不允许新增过渡代码 | AC 全部通过；pytest/ruff 全绿；旧路径 rg 无生产引用 |

每个子阶段提交必须能独立解释为“新增 seam → 迁 Provider → 迁 Consumer → 删除旧入口”中的一个动作，禁止把六个子阶段压成一个不可审查提交。

### 3.10 风险与控制

| 风险 | 控制 |
|---|---|
| 大量路径迁移造成循环导入 | 先建立 Service seam 和架构测试，再移动 Provider |
| Cordis Fiber 异步清理时序与 Gateway shutdown 冲突 | 为 uvicorn、MCP、Cron、Channel 分别增加 lifecycle 测试 |
| FastAPI Router 卸载不完全 | F1 明确启动期冻结；运行时变化标记 restart_required |
| 外部插件被新 Loader 破坏 | 使用 Octo test double + 独立仓库入口回归；不得保留双 Kernel |
| Session/Agent 行为意外变化 | 迁移前后运行同一组 SessionLane、Mailbox、Compaction、协议测试 |
| 一次性重命名难以审查 | 按 F1.1—F1.6 小步迁移，一次提交只处理一个边界 |
| 当前存在 271 个 ruff 历史错误 | F1.1 固化基线；每个子阶段禁止新增，F1.6 清理至 `ruff check` 全绿 |

## 4. 接口定义

### 4.1 Cordis Plugin 入口

ftre 内置和外部 Plugin 使用 Cordis 原生入口，不定义新的上帝对象 API：

```python
from collections.abc import Mapping
from typing import Any

from cordis import Context, Inject


@Inject("tools")
@Inject("sessions")
def plugin(ctx: Context, config: Mapping[str, Any]):
    tools = build_tools(ctx.sessions, config)
    return ctx.tools.register_many(tools)
```

禁止把所有 Service 打包成一个 `FtrePluginApi` 传给 Plugin。

### 4.2 Plugin Manager

项目级 Manager 至少提供：

| 方法 | 输入 | 输出/行为 |
|---|---|---|
| `discover()` | 插件根目录、Builtin Catalog | `PluginManifest[]`；只读元数据，不 import 未启用 External |
| `load_enabled()` | 有效 ConfigSnapshot | 等待 enabled Fiber settle；required 未 ACTIVE 时抛 StartupError |
| `list_status()` | 无 | `PluginStatus[]` 稳定按 Composition/Plugin id 排序 |
| `get_status(plugin_id)` | id | 单个状态；未知 id 返回明确 NotFound |
| `restart(plugin_id)` | optional Plugin id | 调用 Fiber restart；required/Router frozen 情况返回 restart_required 或拒绝 |
| `dispose(plugin_id)` | optional Plugin id | 卸载 Fiber；幂等；required Provider 在运行期拒绝 |
| `close()` | 无 | dispose Manager 拥有的全部 Fiber；由根 Context shutdown 调用 |

`PluginStatus` 至少包含：

```text
id
source
entry
state
required
error
restart_required
```

Manager 只编排 Manifest 与 Fiber，不重新实现 Fiber 状态机。

### 4.3 AgentService

其他能力只能通过 AgentService 使用运行时：

| 方法 | 输入 | 输出/语义 |
|---|---|---|
| `submit(inbound)` | `BusMessage` | `AdmissionResult`；保持 durable admission 和 request_id 幂等 |
| `cancel(session_id, expected_request_id)` | Session 与可选精确 request id | 是否取消；不得误取消新请求 |
| `cancel_queued(session_id, request_id)` | 排队 request | Queue mutation result |
| `wait(session_id, request_id)` | 精确 request | `TurnOutcome`；使用 CompletionRegistry，不轮询 |
| `wait_quiescent(session_id)` | Session | 公开的 quiescent/runtime snapshot |
| `status(session_id)` | Session | phase/activity/active_request_id/queue_depth/can_cancel |
| `is_busy(session_id)` | Session | active turn、compaction 或不可安全管理时为 true |
| `mailbox_snapshot(session_id)` | Session | WebSocket attach 所需只读快照 |
| `list()/get(agent_id)` | 无/id | 活动 Agent Handle 只读视图 |
| `on_created/on_disposed(callback)` | 生命周期 callback | disposer |
| `tool_scope(agent_id)` | Agent id | 受 ToolService 管理的 scope handle |

具体 Python 类型在 F1.3 由现有 `AdmissionResult/TurnOutcome/MailboxSnapshot` 反推并集中导出。不得暴露 `_lanes`、Mailbox Store 或 AgentLoop 私有字段。

### 4.4 外部协议

F1 默认不改变：

- `ftre` CLI 命令和 `ftre gateway` 启动方式。
- FastAPI 现有路由路径、请求体和响应体。
- WebSocket frame、ACK、request_id 和 admission 语义。
- Session JSON 与 Agent 配置目录结构。
- Tool 返回 `str / EventBase / tuple[str, dict]` 的兼容行为。

若实现发现必须改变外部协议，必须先修改本 PRD并追加变更记录，不得在代码中隐式调整。

### 4.5 公共诊断数据结构

以下结构是逻辑字段约束，具体使用 dataclass、TypedDict 或 Pydantic Model 由实现决定；跨 HTTP 边界的结构必须 JSON-safe。

#### PluginStatus

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | Manifest id |
| `source` | string | builtin/external 来源，不含本机 secret |
| `entry` | string | 可读入口；callable 转为稳定 module/name |
| `state` | string | Cordis FiberState 名称 |
| `required` | boolean | 是否影响 Gateway readiness |
| `missing_services` | string[] | PENDING 时缺失依赖 |
| `error_code` | string/null | 稳定错误分类 |
| `error` | string/null | 脱敏后的异常摘要 |
| `restart_required` | boolean | 运行中 registry 与冻结 Host 是否不一致 |

#### RegistryContribution

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | string | tool/command/prompt/route/channel/skill |
| `key` | string | 稳定名称或 method+path |
| `owner` | string | Plugin id |
| `source` | string | builtin/external/system |
| `scope` | string | global/agent/session |
| `active` | boolean | 是否进入当前有效视图 |
| `shadowed_by` | string/null | 被覆盖时的胜出 owner |

#### ConfigSnapshot

| 字段 | 类型 | 说明 |
|---|---|---|
| `revision` | integer | 进程内单调递增；0 表示初始读取 |
| `value` | object | typed config 的 JSON-safe 副本 |
| `source_path` | string | 当前实际配置绝对路径 |
| `content_hash` | string | 用于 watcher 去重，不代替 revision |

#### PromptAssemblyReceipt

| 字段 | 类型 | 说明 |
|---|---|---|
| `agent_id/session_id` | string | 本轮上下文 |
| `sections` | array | name/owner/scope/order/bytes/token_estimate/included/error |
| `total_bytes` | integer | 最终 system prompt 字节数 |
| `estimated_tokens` | integer | 明确标记为估算值，不冒充模型 tokenizer 精确值 |

### 4.6 稳定错误分类

| error code | 产生位置 | 对外语义 |
|---|---|---|
| `CONFIG_INVALID` | ConfigService | 请求无效或启动失败，不写盘 |
| `CONFIG_REVISION_CONFLICT` | ConfigService | HTTP 409；客户端重新读取后再提交 |
| `PLUGIN_ID_CONFLICT` | Catalog | 启动失败，报告冲突来源 |
| `PLUGIN_ENTRY_NOT_FOUND` | Loader | required 失败；optional FAILED |
| `PLUGIN_ENTRY_INVALID` | Loader | entry 不是 Cordis 支持的 Plugin 形态 |
| `PLUGIN_LOAD_FAILED` | Fiber/apply | 保留 cause；按 required 策略处理 |
| `PLUGIN_CLEANUP_FAILED` | Effect dispose | 继续清理其他资源并聚合报告 |
| `SERVICE_CONFLICT` | Cordis/Provider | 同 Service 域出现不允许的重复 Provider |
| `REGISTRY_CONFLICT` | Tool/Command/Http/Channel/Prompt | 同 scope 稳定 key 冲突 |
| `REGISTRY_FROZEN` | HttpService | 注册已记录，但需重启 Host 才生效 |
| `PATH_OUTSIDE_POLICY` | FilesystemService | HTTP 400 或 Tool error，不泄露未授权目录内容 |
| `AGENT_BUSY` | AgentService | 高风险管理操作在 active turn 时拒绝 |

HTTP Router 负责把领域错误映射到既有响应格式；Service 不 import FastAPI HTTPException。

### 4.7 可观测性输出

启动完成时记录一份 Composition Summary：

```text
context id
config revision/hash
required active/total
optional active/pending/failed
service keys
tool/command/route/channel counts
http frozen + restart_required
```

日志输出用于诊断，不作为唯一 API。测试直接查询 Service/Plugin Manager snapshot，避免解析日志文本。

## 5. 验收标准

- [ ] **AC1：Cordis 成为唯一内核。** `python -c "from cordis import Context, Fiber, Service, Inject"` 成功；`rg "FtreContext|FtrePluginApi|ftre\\.plugin\\.kernel" src/ftre` 无匹配。
- [ ] **AC2：目录与导入边界成立。** `tests/architecture` 验证 `platform` 不依赖上层、`services` 不依赖 `features/app`、Consumer 不导入其他能力 `runtime/providers`，测试通过。
- [ ] **AC3：唯一 Composition Root。** `main.py` 不直接构造 SessionManager、AgentLoop、ChannelManager、CronScheduler、McpManager 或 Plugin Manager；默认 Composition 启动时 required Fiber 全部进入 ACTIVE。
- [ ] **AC4：生命周期可逆。** lifecycle 测试注册 Tool、Command、Hook、Channel、MCP 连接、Cron Task 和后台 Task，dispose 对应 Fiber 后贡献消失、资源关闭；重复 dispose 幂等。
- [ ] **AC5：响应式依赖正确。** Consumer 在 Provider 缺失时为 PENDING；Provider 上线后 ACTIVE；Provider dispose 后 Consumer 自动卸载；替换 Provider 后 Consumer 自动重载。
- [ ] **AC6：发现与启用分离。** 测试目录中放置一个带可观测 setup 副作用的外部候选：未写配置时副作用不发生；显式启用后只加载一次；id 冲突和 required 缺失均 fail loud。
- [ ] **AC7：配置单一事实源。** `rg "DEFAULT_CONFIG" src/ftre` 无匹配；`tests/architecture/test_config_ownership.py` 验证只有 `services/config` 可以直接访问根 `config.json`；旧配置样例能被无损加载和保存。
- [ ] **AC8：HTTP Host 顺序正确。** startup 测试断言 Router Contribution 完成后才启动 Server；现有路由清单保持；运行时 Router 变化设置 restart_required，不承诺热生效。
- [ ] **AC9：Agent 核心不变量保持。** SessionLane、Mailbox、ContextGate、CompletionRegistry、Compaction、TurnExecutor 现有测试全部通过；同 Session 最多一个 active turn，turn 与 compaction 不并发，不同 Session 可并行。
- [ ] **AC10：业务协议兼容。** Session CRUD/恢复/fork、EventBus ACK、WebSocket admission、Command、Tool metadata 和 Agent Profile 合并回归测试全部通过。
- [ ] **AC11：内置功能行为保持。** System Prompt 的基础段、Agent Profile、Skill、MCP、ContextGovern 和环境段顺序可预测且无重复；Plan、Team、Schedule 和标题生成的原有关键测试迁移后全部通过。
- [ ] **AC12：Octo 边界保持。** Octo 源码仍位于独立仓库；入口不再引用旧 Kernel；使用 test double 启动/卸载时 Channel、Tool、Router/Hook Contribution 正确注册和清理，且不要求真实 WuKongIM 网络。
- [ ] **AC13：根 Context 无泄漏。** 完整 Composition 启动后 dispose，事件循环中不存在 ftre 创建且未结束的 uvicorn、MCP watcher、Cron、Channel 或 Plugin Task。
- [ ] **AC14：静态与完整测试通过。** 执行 `python -m pytest -q` 全部通过；执行 `python -m ruff check src tests` 全部通过。
- [ ] **AC15：手动启动验收。** 运行 `ftre gateway` 后健康检查、WebSocket 连接、Session 创建、发送一轮消息、取消/等待和正常退出均成功；启动和退出日志包含 Plugin 状态且无 traceback。
- [ ] **AC16：无占位和旧入口。** 目标目录中没有空函数、`TODO` 占位模块或仅为未来设计创建的空包；`api`、`gateway`、`plugin/builtin`、`plugin/kernel` 等旧生产路径在迁移完成后无残留实现或导入。
- [ ] **AC17：Scoped Registry 可审计。** synthetic Plugin 注册 global/agent Tool、Skill、Prompt Section 和 restriction；不同 Agent 得到不同实际 view；schema/receipt 返回 owner/source/scope/shadow；dispose 后原贡献消失且被 shadow 项恢复。
- [ ] **AC18：Filesystem/Workspace 边界成立。** contract tests 覆盖相对路径、绝对路径、`..`、symlink、allowed_dirs、大小限制、原子写失败回滚和 Session workspace 持久化；Plugin/Tool 不直接持有 SessionManager。
- [ ] **AC19：第三方后端 Plugin 公共契约验证。** `tests/architecture/fixtures/audit_plugin` 只使用 `filesystem/workspaces/sessions/agents/tools/skills/system_prompt/http` 公共接口完成只读报告和 Route/Tool 注册；fixture 不导入 `runtime/providers`，不复制三个审计样本仓库代码。
- [ ] **AC20：HTTP 路由兼容。** startup test 对比迁移前冻结的 method/path/status 基线，确认 `/api/traces`、`/api/sessions`、`/api/config`、`/api/cron`、`/api/commands`、`/api/images`、`/api/agents`、`/api/skills`、`/api/mcp` 与 WebSocket `/` 均存在且 owner 唯一。

### 5.1 FR → AC 追踪矩阵

| FR | 主要 AC | 验证重点 |
|---|---|---|
| FR1 | AC1、AC4、AC5 | Cordis 唯一内核、Fiber/Effect/Inject 语义 |
| FR2 | AC2、AC16 | 四层目录、依赖方向、无旧入口 |
| FR3 | AC3、AC8、AC13、AC15 | 唯一 Composition Root 和完整启停 |
| FR4 | AC2、AC17—AC19 | 稳定 Service 与公开契约 |
| FR5 | AC4、AC13、AC17 | Contribution 和资源可逆 |
| FR6 | AC6 | discovery/enable/runtime 分离 |
| FR7 | AC5 | 响应式依赖上线、下线和替换 |
| FR8 | AC7 | Config revision、原子写与单一 Owner |
| FR9 | AC8、AC20 | HttpService/Host/Server 与 Route 兼容 |
| FR10 | AC9、AC10、AC17 | AgentService、Tool Scope 和运行不变量 |
| FR11 | AC9、AC10 | MessageBus 数据面不被 Cordis Event 替换 |
| FR12 | AC11、AC16 | Feature 聚合、行为保持、旧 Builtin 删除 |
| FR13 | AC6 | External 显式装载与入口兼容 |
| FR14 | AC12 | Octo 独立边界和新生命周期 |
| FR15 | AC3、AC6、AC15 | 状态、错误、required fail loud |
| FR16 | AC14—AC16 | 小步迁移、全量质量门禁 |
| FR17 | AC17、AC19 | Tool/Skill/Prompt scoped registry 和 receipt |
| FR18 | AC18、AC19 | Filesystem/Workspace 公共安全边界 |
| FR19 | AC19 | 以 synthetic Plugin 验证优雅度，不迁移外部项目 |

## 6. 测试计划

### 6.1 自动化测试

| 测试层 | 覆盖内容 |
|---|---|
| `tests/unit/platform/plugin_runtime` | Manifest、Catalog、入口解析、冲突和诊断 |
| `tests/contracts/test_config_service.py` | revision、expected-revision、原子写、watcher 去重、unknown field round-trip |
| `tests/contracts/test_filesystem_workspace.py` | PathPolicy、symlink/escape、大小上限、workspace 持久化 |
| `tests/contracts/test_tool_skill_prompt.py` | agent scope、schema provenance、Skill shadow、Prompt Receipt |
| `tests/contracts/test_agent_service.py` | submit/cancel/wait/status/list/events/busy，不暴露 Lane |
| `tests/contracts/test_http_service.py` | exact/prefix/APIRouter、冲突、freeze、restart_required、same-origin |
| `tests/lifecycle` | Fiber 状态、Inject、Effect LIFO、Contribution/Task/连接清理和幂等 dispose |
| `tests/architecture` | 四层依赖方向、禁止私有 Provider 导入、禁止旧 Kernel、synthetic Plugin fixture |
| `tests/startup` | 默认 Composition、阶段顺序、required/optional 失败、HTTP 最后监听、启动回滚 |
| `tests/integration` | Session → MessageBus → AgentService → Channel 完整链路 |
| 现有回归测试 | Session、AgentLoop、MCP、Skill、Tool、Command、Gateway、外部插件 |

### 6.2 Test Double 约束

自动化测试不得依赖真实 LLM、MCP Server、WuKongIM 或网络：

- Fake Plugin 必须能控制 import/apply/cleanup 失败点。
- Fake Service Provider 必须能上线、下线和替换，验证 Consumer Fiber 状态。
- Fake HTTP Server 记录“开始监听”的时刻，证明它晚于 Router freeze。
- Fake MCP/Channel/Cron 记录 Task 和 disposer，证明根 Context dispose 后无泄漏。
- Octo 使用最小入口 test double；真实 Octo 仓库只做兼容验证，不作为单元测试依赖。
- synthetic audit Plugin 只验证公开契约，不复制外部审计项目源码、算法或 UI。

### 6.3 手动验证

1. 使用现有 `~/.ftre/config.json` 启动 `ftre gateway`。
2. 调用 health、config、session、agent、skill、mcp 和 schedule 现有 API。
3. Desktop 建立 WebSocket，创建 Session 并完成一轮 Agent 交互。
4. 触发 cancel、wait、compaction 和正常关闭。
5. 禁用一个 optional Plugin，确认它不运行且其他能力正常。
6. 配置一个缺失 required Plugin，确认 Server 不监听且诊断明确。
7. 在可用环境中启用 Octo，验证其 Channel/Tool 注册；网络依赖不可用时执行 test-double 验收。

### 6.4 每个实施子阶段的门禁

每个 F1.x 子阶段完成后必须执行：

```powershell
python -m pytest -q
python -m ruff check src tests
```

涉及 Gateway、Agent、Session、MCP 或 Octo 的子阶段，还必须执行对应定向测试；失败时停在当前子阶段修复，不继续移动后续模块。

### 6.5 基线与新增失败归属

PRD 草稿时基线为 348 passed、1 个既有 collection warning、271 个 ruff error：

1. F1.1 记录完整 pytest/ruff 输出作为基线附件或 CI artifact。
2. 任一子阶段不得增加新的 test failure、warning 或 ruff error。
3. 现有 ruff 债务按被迁移文件随阶段清理，F1.6 前全部归零。
4. 不能通过扩大 ignore、删除测试或降低 lint 规则制造“通过”。确需规则变化必须修改 PRD 变更记录并说明理由。

## 7. 评审待确认

PRD 进入 `approved` 前必须逐项确认：

| 决策 | 当前提案 | 未确认的影响 |
|---|---|---|
| D1 单包目录 | 接受 `app/platform/services/features`，不立即采用 DSH 多 Distribution | 决定全部目标 import path |
| D2 Octo | 推荐直接改为 Cordis Plugin；若使用兼容层，必须写明删除子阶段且 AC1 最终仍无旧 API | 影响外部仓库联动与 F1.5 |
| D3 Router | 启动期可组合；运行期变化设置 restart_required；不实现 HMR | 决定 HttpService freeze 契约 |
| D4 C4 状态 | C4 只保留历史实现记录，F1 不复用其 Kernel | TODO 当前同时有 C4/F1 in_progress，需要流程决策 |
| D5 cordis-py 来源 | 锁定可重复安装的 0.4.x 发布物；不跟随未固定本地源码 | 决定 pyproject 和 CI 安装方式 |
| D6 F1 扩展范围 | 只承诺 Backend Behavior/Provider Plugin；Client/Recovery Extension 明确非目标 | 防止把前端插件和灾难恢复塞入 F1 |
| D7 Service 契约 | 以第 3.4 节为开发基线；方法名或行为变更必须回写 PRD | 决定 Provider/Consumer 是否可并行开发 |
| D8 required 清单 | foundation/core/state/adapters/context-govern/http-server required，其余 feature optional | 决定启动 fail-loud 范围 |

### 7.1 approved 前检查清单

- [x] D1—D8 均有明确结论；迁移期保留旧 Kernel 兼容 API，新代码只依赖 `cordis` 公共面。
- [x] FR1—FR19 每条都有 Owner、实现阶段和对应 AC。
- [x] AC1—AC20 均可在无真实网络/API key 条件下执行，手动 AC15 除外。
- [x] 目标目录中的每个文件都能映射现有代码或本 PRD 明确的新 Service，不创建纯占位目录。
- [x] Service key、scope、冲突、freeze、revision 和 failure semantics 已统一。
- [x] C4 的历史实现仅保留兼容入口，F1 作为当前唯一新架构实施阶段。
- [x] `cordis-py` 依赖已声明；当前离线环境同时提供同契约兼容 fallback 以保证测试可运行。
- [x] Octo 保持外部仓库和迁移期兼容入口，不移动其业务源码到主仓库。

## 8. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-20 | 创建 PRD 草稿壳子，登记背景和访谈问题 | 先建立独立 F1 需求基线 |
| 2026-08-20 | 基于代码审查和 DSH/cordis-py 研究补全目标、FR、技术方案、迁移阶段、接口和 AC | 将访谈结论收敛成可评审、可执行的第一版 PRD；状态仍为草稿，尚未定稿 |
| 2026-08-20 | 详细化四层文件树、Service 方法、scope/provenance、Plugin 类型、Composition、失败矩阵、HTTP 所有权、迁移门禁和 FR→AC 追踪 | 根据用户要求将架构纲要扩展为可直接指导实现和评审的详细规格；三个外部审计项目仍只作为验证样本，不进入实现范围 |
| 2026-08-20 | PRD 由草稿批准为 `approved`；D1-D8 按用户授权冻结，进入整份 F1 连续开发 | 用户要求直接完成整个 PRD，不在阶段之间停下来请求继续许可 | AC1-AC20 持续重跑 |
| 2026-08-20 | 完成 F1.2：ConfigService revision/atomic update/watch、Filesystem/Workspace 边界、HttpService route registry/freeze、Composition 路由注册，并移除配置导入副作用 | 将配置、路径和 Host 注册面从模块全局行为收敛为可测试的 Service | AC1、AC2、AC3、AC5、AC6、AC7、AC8、AC12 已由契约/架构测试覆盖；现有回归保持通过 |
