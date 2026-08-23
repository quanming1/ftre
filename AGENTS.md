# ftre 后端协作说明

本文件是 `E:\\ftre` 后端仓库的协作约束。后端代码位于 `src/ftre/`；桌面端和文档站
仍是严格边界。`ftre-agent-core` 是独立仓库；只有明确授权的跨仓库阶段（如 F7/C1）
才可在 Core 自有 feature 分支同步修改，禁止把 Core 文件复制进本仓库。

## 项目边界

| 组件 | 路径/仓库 | 责任 |
| --- | --- | --- |
| ftre Gateway | `src/ftre/` | 有状态长驻进程：组合 Service、加载 Plugin、管理 Session、Agent、Channel 和 HTTP/WS |
| Agent Core | `E:\\ftre-agent-core` | 无状态 ReAct/LLM/Tool 算法库，被 Gateway import，不独立部署 |
| Desktop | `E:\\binn\\ftre-desktop` | Electron + React 客户端；本仓库只维护兼容 API，不改客户端 |
| 外部插件 | `C:\\Users\\蒋全明\\.ftre\\plugins\\` | 显式配置后由 Plugin Runtime 发现和加载；Octo 仓库保持独立 |
| 用户配置 | `C:\\Users\\蒋全明\\.ftre\\` | `config.json`、Agent 配置、插件目录和运行时数据 |

## 核心指导思想：轻内核 + Plugin-first（MANDATORY）

这是 ftre 架构和重构决策的最高优先级原则。目录分层、Service 抽象、Hook、Package 拆分和
Composition 都必须服务于这一原则；局部实现与本节冲突时，应把局部实现登记为架构债务，
不得把它继续复制到新代码。

### 轻内核

Kernel 只提供与业务无关的运行机制：

```text
Kernel
├─ Context / Service 注册与解析
├─ Plugin Manifest / Discovery / Loader / Manager
├─ Hook 注册、作用域分发和取消
├─ Fiber / Effect 生命周期与资源清理
└─ 启动诊断、依赖缺失和冲突报告
```

Kernel 不得识别或 import Queue、pending、Compaction、Command、Session title、Schedule、
Team、MCP、Skill、WebSocket payload、Agent prompt、Tool policy 等产品或业务概念。
`kernel/` 中出现这些词汇或实现依赖时，默认视为分层错误。

Agent Runtime 是最小执行能力，但仍是由 Provider Plugin 装配的业务 Service，不属于 Cordis
Kernel。它只负责 `InboundMessage → Turn → Reasoning/Tool → Assistant Output`，不负责
Queue、Command、Compaction、Channel 协议和 Feature 行为。

### Plugin-first

- 每项完整业务能力必须有唯一 Plugin Owner。Plugin 负责创建、注册、启停和清理能力。
- 有状态、可复用能力通过稳定 Service key 提供；消费者只能通过 Cordis `inject` 获取公开
  Service，不得 import/实例化另一个能力的 Provider、Repository 或 Runtime 私有实现。
- 可选时机行为通过 Hook 注册；核心执行方只发布通用生命周期边界，不检查某个 Feature
  是否安装，也不持有 Feature 实现。
- “内置”只表示默认 Composition 会加载该 Plugin；必选能力同样必须经过 Plugin 生命周期，
  不能由 Bootstrap 手工 `new` 后绕开 Loader、Fiber 和 Effect。
- 一个 Plugin 可以包含多个内部模块并提供一个 Service；Plugin-first 不等于每个文件、类或
  函数都包装成 Plugin，也不要求所有 Plugin 立即拆成独立 PyPI 包。
- Package 是发布边界，Plugin 是装配/生命周期边界，Service 是运行时能力，Hook 是扩展时机；
  四者不得混为一层。

### 简洁性硬约束

- 不为单一实现新增 `*Port`、`*Coordinator`、透传 Facade 或转换层；直接 Inject 命名 Service
  的窄公开方法。只有两个真实实现、跨包稳定契约或明确测试替身需求时才引入 Protocol。
- 禁止全局 setter、`bind_legacy_*`、Service Bag、静态 Builder、兼容 alias 和第二
  Composition Owner。
- Bootstrap 只负责进程启动/关闭和调用 Composition，不承载业务装配、Feature 判断或
  Service 之间的手工绑定。
- Composition Root 只声明 Plugin 清单和 required/optional 关系，不直接构造业务对象图。
- Plugin 的 Route、Hook、Listener、Task、Thread 和资源必须全部绑定 Effect；卸载一个 Plugin
  后，它的行为和资源必须完整消失，基础 Agent Turn 仍可按声明的能力集合运行。

### 能力归属判断

新增或修改能力前必须能直接回答：

1. 哪个 Plugin 创建并销毁它？
2. 它提供哪个 Service，或监听哪些 Hook？
3. 哪些消费者通过 `inject` 使用它？
4. 不加载或卸载该 Plugin 时，系统行为是什么？

如果答案需要同时解释多个 Coordinator、Port、Facade、全局绑定或 Bootstrap 字段，先停止
实现并重新划定 Owner。详细阶段契约见 `docs/prd/PRD-F13-plugin-first-kernel.md`。

## 当前架构：四层 + 一个 Composition Root

```text
src/
└─ ftre/
   ├─ app/                         # 进程边界：CLI、Gateway 启动、FastAPI Host、uvicorn
   │  └─ gateway/
   │     ├─ composition.py         # 唯一 Composition Root：声明默认 Plugin 清单
   │     ├─ bootstrap.py            # 启动/关闭顺序；不承载业务规则
   │     └─ http/                   # Host 与服务器适配
   ├─ platform/                    # 轻内核：运行时机制，不承载产品能力
   │  └─ plugins/            # Manifest、Discovery、Loader、Manager、Diagnostics
   ├─ services/                    # 有状态公共能力；每个能力由 Service + Provider Plugin 组成
   │  ├─ config/ filesystem/ http/
   │  ├─ messaging/{bus,channel}/
   │  ├─ session/ agent/ tools/ workspace/
   │  ├─ command/ attachment/ observability/
   │  └─ system_prompt/
   └─ features/                    # 产品行为 Plugin，可选地提供 Feature Service
      ├─ skill/ mcp/ plan/ team/ schedule/
      └─ context_govern/
```

### Service 与 Plugin 的区别

- **Service** 是可被其他模块消费的、有稳定 key 的运行时能力，例如 `sessions`、`tools`、
  `message_bus` 和 `http`。Service 保存状态并暴露窄接口，不负责自身的全局装配。
- **Provider Plugin** 是 Service 的生命周期适配器，通常位于同一目录的 `plugin.py`，通过
  `provide = (...)` 声明输出、`inject = (...)` 声明依赖，并用 `ctx.effect(...)` 注册可逆清理。
- **Feature Plugin** 位于 `features/`，拥有 Skill、MCP、Team 等产品行为；它只能消费公开
  Service，不得访问 AgentLoop、Session 存储等私有实现。
- Plugin 入口统一是 `module:attribute` 指向 `apply`。Agent runtime 的 Provider/Plugin
  边界已经建立；任何仍由 Bootstrap 直接创建或复制 Service 句柄的代码都属于 F13 存量
  架构债务，不是新 Provider/Service 可以仿照的模式。目标是由 Agent Provider Plugin
  通过 Inject 创建、provide 并清理运行时。

## 启动与生命周期

```text
ftre.main
  → app.gateway.bootstrap
  → build_composition
  → PluginManager / PluginLoader
  → cordis.Context + Fiber
  → Provider Services
  → Feature Plugins / 外部 Plugins
  → FastAPI Host + WebSocket Channel + AgentLoop
```

1. `composition.py` 是默认 Plugin 清单的唯一事实源；新增内置能力先加入清单，再写对应测试。
2. `PluginDiscovery` 只解析 Manifest，不导入未启用的外部模块；外部插件必须在用户配置中显式启用。
3. `Context` 按声明限制 Service 访问；依赖缺失时 Fiber 保持 `PENDING`，依赖出现后重新激活。
4. Plugin 的所有注册、路由、事件监听、后台任务和资源都必须绑定 `ctx.effect`，保证 unload/close
   可逆且幂等。
5. 必选 Plugin 启动失败会产生诊断并阻止 Gateway 启动；可选 Plugin 失败只记录状态。
6. `ftre.plugin`、`ftre.agent`、`ftre.session`、`ftre.bus`、`ftre.channel`、`ftre.command`、
   `ftre.tools`、`ftre.api`、`ftre.config`、`ftre.mcp` 等旧目录/根模块已退役。新代码不得
   依赖旧 Kernel 或兼容入口，新的 Service/Feature 必须放入四层目录。

## Agent 数据面不变量

当前运行数据面仍是 `Channel → EventBus → AgentLoop ingress → ftre-inbox（可选）→ AgentService/TurnExecutor`；
F13 的目标数据面是 `Channel Plugin → Ingress → CommandService 或 InboxService → AgentService`，
AgentLoop 不再拥有 Command/Inbox 的业务分流：

- Command 必须在接入裁决层通过 Inject 的 CommandService 旁路执行；普通输入由 `ftre-inbox`
  持久接纳后才交给 AgentService。
- 不同 Session 可并行；同一 Session 同时最多一个 active turn；队列 worker 由 `ftre-inbox` 独立拥有。
- `ftre-inbox` 的 `next-turn`/`next-step`、pending、容量和恢复不进入 AgentService；`messages` 是聊天历史，
  `CompletionRegistry` 仅保存进程内等待。
- Channel 负责接入与协议，EventBus 只负责传输，`ftre-inbox` 负责 admission、队列串行化和
  claim，AgentService 负责 active Turn；未安装 Inbox 时普通输入明确返回 capability error，
  不回退旧 Lane。

## 插件开发约定

```python
from cordis import Context

inject = ("http",)
provide = ("example",)


def apply(ctx: Context, config: dict | None = None):
    service = ExampleService(config or {})
    ctx.provide("example", service)
    ctx.effect(lambda: service.close, label="example:close")
```

- 公共 Service key、Manifest id 和路由路径必须稳定；冲突由 Runtime 拒绝。
- Plugin 入口必须使用 `module:attribute`；旧 `module.Class` 和 `setup(ctx, config)` 入口不再支持。
- 注释要求：每个新模块说明层级职责；每个公共 Service/Plugin、生命周期方法和非显然并发/清理逻辑
  必须有 docstring 或近邻注释，解释“为什么”和边界，不重复代码字面含义。
- 不在 Plugin 中创建全局 FastAPI；通过 `HttpService.register_router` 贡献路由。
- 不在 Feature 中 import 另一个 Feature 的私有模块；跨能力协作通过 Service key 或事件完成。

## 开发、测试与运行

- Python 3.12；安装：`pip install -e .`，开发依赖：`pip install -e .[dev]`。
- 启动：`ftre gateway`；后台模式：`ftre gateway --background`；状态/停止：`ftre gateway status|stop`。
- 必跑验证：`python -m pytest -q`、`python -m ruff check src tests`、`git diff --check`。
- 架构测试位于 `tests/architecture/`、`tests/contracts/`、`tests/startup/` 和 `tests/lifecycle/`。
- 进入仓库或提交前必须阅读：`docs/COMMIT.md`、`docs/PROCESS.md`、`docs/TODO.yaml`。

## Git 与 PRD 约束

- 默认从 `develop` 创建 `feature/<阶段id>-<任务>`；禁止直接提交 `develop`/`master`。
- 提交格式：`<type>(<scope>): <中文主题>`；`feat/fix/prd/todos` 的 scope 必须是 `docs/TODO.yaml`
  中真实阶段 id，其他规则以 `docs/COMMIT.md` 为准。
- 任何行为变更先更新对应 `docs/prd/PRD-*.md`，按 `docs/PROCESS.md` 完成“立项→评审→开发→验证→收尾”。
- 未经用户明确要求，不执行 commit、push、merge、回滚或跨仓库修改。
