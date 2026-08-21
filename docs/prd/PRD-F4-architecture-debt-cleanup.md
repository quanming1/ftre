# PRD-F4-架构债务清理与单一 Owner 收敛

> F1/F2/F3 已建立四层架构、Cordis Runtime、Service/Feature Owner，并删除旧
> Plugin Kernel/API。F4 处理剩余的“迁移完成但旧壳仍在”的架构债务：删除旧数据面
> import shim，收回仍停留在根目录的真实实现，消除重复 Owner、通配符转发和死兼容 API。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F4 |
| 名称 | 架构债务清理与单一 Owner 收敛 |
| 状态 | 开发中 |
| 创建日期 | 2026-08-21 |
| 定稿日期 | 2026-08-21 |
| 验收日期 | — |
| 关联文档 | `docs/TODO.yaml` 阶段 F4；`AGENTS.md`；`docs/PROCESS.md`；F2/F3 PRD 与执行报告 |

## 1. 背景与目标

### 1.1 当前债务盘点

| 债务类别 | 当前证据 | 问题 | 目标 Owner |
|---|---|---|---|
| Agent 旧壳 | `src/ftre/agent/*` | `import *`、`sys.modules` 替换和 lazy export；旧目录看起来像业务 Owner | `services/agent/profile`、`services/agent/runtime` |
| 数据面旧壳 | `src/ftre/session`、`bus`、`channel`、`command`、`tools` | 旧路径仍被测试和部分工具使用，维护两套 import 表面 | `services/session`、`services/messaging`、`services/command`、`services/tools` |
| Config 重复 Owner | `src/ftre/config.py` 与 `services/config/*` | AgentConfig/LLMConfig、原始 config 文件和路径事实源混在旧根模块 | `services/config` + `services/agent/config.py` |
| Trace 重复 Owner | `src/ftre/trace_store.py` 被 `services/observability/trace/service.py` 反向导入 | Service 层依赖根目录实现，无法证明 Trace Service 是唯一 Owner | `services/observability/trace/store.py` |
| MCP 分裂 | `src/ftre/mcp/*` 是真实连接/适配实现，`features/mcp/*` 只有状态 Service 和转发壳 | Feature Plugin 与 MCP 传输实现不在同一能力边界，旧根包仍是实际 Owner | `features/mcp/*` |
| Feature 通配符壳 | `features/mcp/adapter.py`、`mcp/config.py`、`plan/tool.py`、`schedule/tool.py`、`team/profile.py`、`team/tools.py` | Feature 目录存在但文件只转发到旧/共享实现，职责不清 | 删除死壳；真实实现归属明确的 Service/Feature |
| HTTP 死兼容 API | `services/http/dependencies.py`、`register_compat_snapshot`、`kind="compat"` | 旧 aggregate API 已删除，遗留接口却继续暗示双路由体系 | `HttpService` 只保留正式 Route/WebSocket Path |
| Gateway 边界错位 | `src/ftre/gateway/runtime.py` 被 CLI 使用，真正进程边界在 `app/` | 进程管理不在 App 层，Composition 与 CLI 入口分散 | `app/gateway/process.py` |
| 图片基础设施错位 | `src/ftre/utils/image_store.py` 被 Session/Tool/WS/MCP 多处直接引用，`services/attachment/store.py` 仍是空转发 | 附件存储没有单一基础设施 Owner | `services/attachment/store.py` |

### 1.2 目标

> 删除迁移完成后的旧目录和转发壳；把真实实现放回唯一 Service/Feature/App Owner；让
> 生产代码、测试和文档只使用新路径，最终通过架构门禁证明“一个能力只有一个事实源”。

### 1.3 非目标

1. 不修改 Desktop、`ftre-agent-core`、Octo 独立仓库或客户端协议。
2. 不删除仍有业务语义的协议兼容字段（例如 WebSocket payload 的客户端字段）；只删除
   Python 模块路径和内部架构兼容壳。
3. 不在本阶段重写 AgentLoop、SessionLane、Compaction、MCP 协议或 Trace 数据格式；只做
   Owner 迁移和调用方改造。
4. 不引入新的插件市场、远程安装、进程隔离、数据库或 HMR。
5. `gateway/runtime.py` 与图片存储的归位必须保持现有 CLI 和文件协议行为，不扩展产品能力。

## 2. 需求范围

### 2.1 功能需求

- [ ] **FR1：债务基线与 Owner 清单。** 建立机器可执行的架构测试，扫描 `src/ftre` 中的
  `import *`、`sys.modules[__name__]`、根目录旧包和 Service 反向依赖；每个债务项必须标注
  唯一 Owner、迁移提交和删除条件。
- [ ] **FR2：删除 Agent 旧壳。** 迁移全部测试、fixtures 和内部引用后，删除
  `src/ftre/agent`；生产代码和测试只从 `services/agent` 导入。
- [ ] **FR3：删除数据面旧壳。** 删除 `src/ftre/session`、`bus`、`channel`、`command`、
  `tools` 中仅用于转发的模块以及 `services/session/compat.py`；`channel/test_channel.py`
  移入 `tests/`，不能留在生产包内。
- [ ] **FR4：Config Owner 收敛。** 将原始配置路径、JSON 存储和快照能力归入
  `services/config`，将 `AgentConfig`、`LLMConfig`、`ContextConfig` 与解析函数归入
  `services/agent/config.py`；删除 `src/ftre/config.py`，禁止新 Owner 反向导入它。
- [ ] **FR5：Trace Owner 收敛。** 将 `src/ftre/trace_store.py` 的真实 SQLite 实现迁入
  `services/observability/trace/store.py`，更新 TraceService、Agent Runtime、路由和测试，
  删除根模块。
- [ ] **FR6：MCP Owner 收敛。** 将 `src/ftre/mcp/{config,manager,adapter}.py` 的实际能力
  迁入 `features/mcp` 的明确模块（配置、连接生命周期、Tool Adapter），由 MCP Feature
  Plugin 负责创建和关闭；删除根 `mcp` 包及 Feature 通配符转发。
- [ ] **FR7：Feature 空壳清理。** 删除或改造 `features/mcp/adapter.py`、`features/mcp/config.py`、
  `features/plan/tool.py`、`features/schedule/tool.py`、`features/team/profile.py`、
  `features/team/tools.py`；每个保留文件必须拥有实际职责，禁止单行 `import *`。
- [ ] **FR8：HTTP 兼容表面清理。** 删除未使用的 `ApiDependencies`、`register_compat_snapshot`
  和 `kind="compat"`；WebSocket `/` 使用正式的 `register_websocket_path`（或等价正式 API），
  路由快照不再伪装成 legacy/compat surface。
- [ ] **FR9：Gateway 进程边界归位。** 将 `GatewayRuntime`、`GatewayStatus` 和后台进程文件
  操作迁入 `app/gateway/process.py`，CLI 只依赖 App 层入口；`src/ftre/gateway` 删除或只
  保留明确的空包说明。
- [ ] **FR10：Attachment 基础设施归位。** 将 `utils/image_store.py` 的真实读写实现迁入
  `services/attachment/store.py`，Session/Tool/WS/MCP 通过 Attachment/Store 公共接口使用，
  删除 root `utils.image_store` 的业务 Owner 身份。
- [ ] **FR11：严格新路径。** 清理后 `src/ftre/app`、`platform`、`services`、`features`、
  `cordis` 及测试不得导入已删除旧路径；禁止新增 `import *` 和 `sys.modules` 模块替换。
- [ ] **FR12：可回滚分片。** 按 Agent/Data Shim、Config/Trace、MCP、HTTP/Feature、App/Attachment
  五个切片提交；每个切片都有迁移测试和独立回滚边界。

### 2.2 非功能需求

- **单一事实源**：同一 Service/Feature 只允许一个生产实现和一个公共 Owner。
- **可读性**：公共模块必须显式导出 API；禁止使用通配符 re-export 作为长期结构。
- **运行时稳定性**：现有 Gateway health、WebSocket attach、Session/Agent/Tool/MCP/Trace
  行为保持通过。
- **数据安全**：不改变 Session JSON、Trace SQLite、附件路径和 Gateway 状态文件格式。
- **边界安全**：删除旧模块后，错误必须在导入/启动阶段显式失败，不能静默 fallback 到旧实现。

## 3. 目标结构与技术方案

### 3.1 目标文件树（与本阶段相关）

```text
src/ftre/
├─ app/
│  └─ gateway/
│     ├─ composition.py
│     ├─ bootstrap.py
│     ├─ process.py              # GatewayRuntime / GatewayStatus
│     └─ http/
├─ platform/
│  └─ plugin_runtime/
├─ services/
│  ├─ config/
│  │  ├─ paths.py
│  │  ├─ store.py
│  │  ├─ service.py              # 原始配置快照与持久化
│  │  └─ loader.py               # gateway 配置读取
│  ├─ agent/
│  │  ├─ config.py               # AgentConfig / LLMConfig / ContextConfig
│  │  ├─ profile/
│  │  └─ runtime/
│  ├─ observability/trace/
│  │  ├─ store.py                 # SQLiteTraceExporter 实现
│  │  ├─ service.py
│  │  └─ router.py
│  ├─ attachment/
│  │  ├─ store.py                 # 图片/附件读写实现
│  │  ├─ service.py
│  │  └─ router.py
│  └─ ...
├─ features/
│  └─ mcp/
│     ├─ config.py
│     ├─ connection.py             # MCP 连接生命周期
│     ├─ adapter.py                # MCP Tool → ftre Tool
│     ├─ service.py
│     ├─ plugin.py
│     └─ router.py
└─ utils/                          # 只保留真正通用、无产品 Owner 的纯工具
```

### 3.2 迁移原则

1. 先建立新 Owner 的公开 API，再迁移调用方和测试，最后删除旧文件；禁止先删后补兼容。
2. 对每个旧模块执行 `rg` 生产/测试引用扫描，并用 AST 架构测试阻止回流。
3. 迁移过程中不使用 `sys.modules` 替换或 `import *`；必须显式导入和显式 `__all__`。
4. Service 只暴露窄接口；Feature Plugin 负责注册行为和 Effect 清理；App 只负责进程/Host。
5. 如果某个旧实现无生产调用者，先补“无生产引用”证据，再直接删除，不再保留空壳。

## 4. 接口与导入契约

### 4.1 公共导入契约

```python
from ftre.services.agent.config import AgentConfig, ContextConfig, LLMConfig
from ftre.services.observability.trace.store import SQLiteTraceExporter
from ftre.features.mcp.adapter import build_mcp_tools
from ftre.app.gateway.process import GatewayRuntime
```

以下路径在 F4 完成后必须导入失败：

```text
ftre.agent.*
ftre.session.*
ftre.bus.*
ftre.channel.*
ftre.command.*
ftre.tools.*
ftre.config
ftre.trace_store
ftre.mcp.*
```

### 4.2 生命周期契约

- MCP 连接、Trace Store、Attachment Store 和 Gateway Process 的创建/关闭必须有明确 Owner。
- Plugin 产生的连接、watcher、工具和路由必须绑定 Fiber Effect。
- 所有 `close/stop/dispose` 必须幂等；失败时必须清理已创建的部分资源。

## 5. 验收标准

- [ ] **AC1：旧数据面删除。** `src/ftre/agent`、`session`、`bus`、`channel`、`command`、
  `tools` 中不存在生产 Python 模块；全量测试不再导入这些路径。
- [ ] **AC2：Config 单一 Owner。** `src/ftre/config.py` 删除；所有生产/测试导入改为
  `services/config` 或 `services/agent/config`；配置解析测试全部通过。
- [ ] **AC3：Trace 单一 Owner。** `src/ftre/trace_store.py` 删除；Trace Service 不反向依赖
  根模块；Trace CRUD、purge 和启动测试通过。
- [ ] **AC4：MCP 单一 Owner。** `src/ftre/mcp` 删除；MCP 配置解析、连接、工具转换、热重载
  和 Feature Plugin cleanup 测试通过。
- [ ] **AC5：无 Feature 通配符壳。** `features` 下不再存在只做 `import *` 的模块；所有
  Feature 文件都有实际职责或被删除。
- [ ] **AC6：HTTP 正式注册。** `HttpService` 不再包含 legacy/compat snapshot API；路由快照
  仍包含 `/api/health` 和 WebSocket `/`，但 kind/owner 使用正式定义。
- [ ] **AC7：App 边界归位。** CLI、后台 start/status/stop/restart/logs 全部从
  `ftre.app.gateway.process` 工作；旧 `ftre.gateway` 不再承载实现。
- [ ] **AC8：Attachment Owner。** 图片保存、读取、data URL 转换统一由 `services/attachment`
  提供；Session/Tool/WS 测试通过且路径/安全策略不变。
- [ ] **AC9：架构门禁。** AST 扫描禁止旧导入、`import *`（测试 fixture 除外）和
  `sys.modules[__name__]`；每个删除路径有明确失败测试。
- [ ] **AC10：完整回归。** `python -m pytest -q`、`python -m ruff check src tests`、
  `git diff --check`、Gateway health、WebSocket attach、Composition close 全部通过。
- [ ] **AC11：收尾。** 五个迁移切片分批提交，PRD/TODO/CHANGELOG/执行报告同步，分支干净。

## 6. 测试计划

### 6.1 架构测试

- `tests/architecture/test_f4_no_legacy_packages.py`：旧包、旧根模块和空壳删除。
- `tests/architecture/test_f4_single_owner.py`：Owner 导入方向、无 wildcard/sys.modules 替换。
- `tests/architecture/test_f4_http_registry.py`：正式 HTTP/WebSocket route kind 和 owner。

### 6.2 契约与行为测试

- Agent/Session/Bus/Channel/Command/Tool 原有测试迁移到新 Owner。
- `tests/contracts/test_f4_config_owner.py`：配置路径、快照、Agent 配置解析。
- `tests/contracts/test_f4_trace_owner.py`：Trace store/service identity 与 CRUD。
- `tests/contracts/test_f4_mcp_owner.py`：MCP config、adapter、连接池和 cleanup。
- `tests/contracts/test_f4_attachment_owner.py`：图片保存、data URL、路径安全。

### 6.3 手动验证

1. 启动 `start_gateway(config={})`，确认 `/api/health` 返回 200。
2. 使用 Composition Host 建立 WebSocket，发送 attach 并收到 `reply_snapshot`。
3. 执行 `ftre gateway status|stop|restart|logs --no-follow`，确认 CLI 行为不变。
4. 执行 Composition 重复 close，确认无残留任务、连接、watcher 或临时文件异常。

## 7. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-21 | 初始草案：盘点旧数据面 shim、Config/Trace/MCP 重复 Owner、HTTP/Feature 死壳、Gateway/Attachment 错位 | F3 后仍有大量迁移兼容结构，旧目录继续制造错误的 Owner 认知 |
| 2026-08-21 | 评审通过，进入 F4 开发；冻结 FR/AC 与五个迁移切片 | 用户确认开始执行，后续实现必须按本 PRD 验收 |
| 2026-08-21 | 开发启动；按切片迁移旧数据面、Config/Trace、MCP、HTTP/Feature、Gateway/Attachment | 用户明确要求立即执行，不保留已完成迁移的旧路径兼容壳 |
