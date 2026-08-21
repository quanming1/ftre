# ftre 后端协作说明

本文件是 `E:\\ftre` 后端仓库的协作约束。后端代码位于 `src/ftre/`；桌面端、文档站和
Agent 核心库是独立仓库，本仓库任务不得修改它们。

## 项目边界

| 组件 | 路径/仓库 | 责任 |
| --- | --- | --- |
| ftre Gateway | `src/ftre/` | 有状态长驻进程：组合 Service、加载 Plugin、管理 Session、Agent、Channel 和 HTTP/WS |
| Agent Core | `E:\\ftre-agent-core` | 无状态 ReAct/LLM/Tool 算法库，被 Gateway import，不独立部署 |
| Desktop | `E:\\binn\\ftre-desktop` | Electron + React 客户端；本仓库只维护兼容 API，不改客户端 |
| 外部插件 | `C:\\Users\\蒋全明\\.ftre\\plugins\\` | 显式配置后由 Plugin Runtime 发现和加载；Octo 仓库保持独立 |
| 用户配置 | `C:\\Users\\蒋全明\\.ftre\\` | `config.json`、Agent 配置、插件目录和运行时数据 |

## 当前架构：四层 + 一个 Composition Root

```text
src/
├─ cordis/                         # cordis-py 公共契约的离线兼容实现
└─ ftre/
   ├─ app/                         # 进程边界：CLI、Gateway 启动、FastAPI Host、uvicorn
   │  └─ gateway/
   │     ├─ composition.py         # 唯一 Composition Root：声明默认 Plugin 清单
   │     ├─ bootstrap.py            # 启动/关闭顺序；不承载业务规则
   │     └─ http/                   # Host 与服务器适配
   ├─ platform/                    # 运行时基础设施，不承载产品能力
   │  └─ plugin_runtime/            # Manifest、Discovery、Loader、Manager、Diagnostics
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
- `factory.py` 只允许表达内部对象组装（当前仅 Agent runtime 使用）；它不是 Service，也不是
  Plugin 的入口。Plugin 入口统一是 `module:attribute` 指向 `apply`。

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
3. `PluginContext` 按声明限制 Service 访问；依赖缺失时 Fiber 保持 `PENDING`，依赖出现后重新激活。
4. Plugin 的所有注册、路由、事件监听、后台任务和资源都必须绑定 `ctx.effect`，保证 unload/close
   可逆且幂等。
5. 必选 Plugin 启动失败会产生诊断并阻止 Gateway 启动；可选 Plugin 失败只记录状态。
6. `ftre.plugin`、`ftre.agent`、`ftre.api` 等旧目录已退役。新代码不得依赖旧 Kernel 或兼容入口，
   新的 Service/Feature 必须放入四层目录。

## Agent 数据面不变量

数据面仍是 `Channel → EventBus → AgentLoop → SessionLane → TurnExecutor`，但由 Service 组合提供：

- 不同 Session 可并行；同一 Session 同时最多一个 active turn。
- turn 与 compaction 不并发；pending 领取采用 at-most-once。
- `MailboxStore` 只持久化 pending；`messages` 是聊天历史；`CompletionRegistry` 仅保存进程内等待。
- Channel 负责接入与协议，EventBus 负责传输，AgentLoop 负责路由，SessionLane 负责单会话串行化。

## 插件开发约定

```python
from cordis import PluginContext

inject = ("http",)
provide = ("example",)


def apply(ctx: PluginContext, config: dict | None = None):
    service = ExampleService(config or {})
    ctx.provide("example", service)
    ctx.effect(service.close, label="example:close")
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
