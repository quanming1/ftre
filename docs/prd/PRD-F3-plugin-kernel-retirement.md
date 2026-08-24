# PRD-F3-旧 Plugin Kernel 与兼容入口退役

> F2 已完成核心数据面迁移。F3 处理 AC16 留下的兼容窗口：迁移旧 Kernel/Builtin/API 的测试与调用面，确认新 Cordis Runtime 和 Feature Plugin 已覆盖后删除旧实现。本阶段不保留外部旧插件兼容入口，所有 Plugin 统一使用公开 Cordis `apply` 契约。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F3 |
| 名称 | 旧 Plugin Kernel 与兼容入口退役 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-21 |
| 定稿日期 | 2026-08-21 |
| 验收日期 | 2026-08-21 |
| 关联文档 | `docs/TODO.yaml` 阶段 F3；`AGENTS.md`；`docs/PROCESS.md`；`docs/prd/PRD-F1-backend-plugin-refactor.md`；`docs/prd/PRD-F2-data-plane-migration.md` |

## 1. 背景与目标

### 1.1 背景

F1/F2 已经让新 Composition、Cordis Context/Fiber、Service、Feature Plugin 和数据面 Owner 进入真实启动路径，但 `src/ftre/plugin/kernel`、`src/ftre/plugin/builtin`、`src/ftre/api/routes.py` 仍被历史测试和兼容入口保留。它们已经不参与新 Gateway 装配，却继续制造两套 Plugin API、两套事件上下文和一个旧聚合 HTTP 表面。

### 1.2 目标

> 迁移旧 Plugin Kernel/Builtin/API 的测试与调用面到 Cordis/Feature/Service 契约，删除旧实现目录，确保新生产代码、测试和外部 Plugin 适配只依赖公开新 API。

### 1.3 非目标

1. 不修改 Desktop、`ftre-agent-core`、Octo 独立仓库或客户端协议。
2. 不实现 Plugin Marketplace、远程安装、进程隔离、HMR 或完整 Desktop Extension。
3. 不重写 F2 已迁移的 Agent、Session、Bus、Channel、Tool 算法。
4. 不实现外部旧插件兼容；`setup(ctx, config)`、`module.Class` 和 `ftre.plugin` 入口均不在目标范围内。

## 2. 需求范围

### 2.1 功能需求

- [x] **FR1：Hook 契约统一。** Agent Runtime 只使用 `services/agent/runtime/hooks.py` 和 Cordis Context Event；旧 `ftre.plugin.kernel.events` 不再被生产代码或新测试导入。
- [x] **FR2：旧 Builtin Plugin 迁移。** Skill、MCP、Plan、Team、Schedule、ContextGovern、SessionTitle 的测试改为验证 `plugins/builtin/*` 或 `services/*` 的新 Plugin/Service，删除 `src/ftre/plugin/builtin`。
- [x] **FR3：旧 Kernel 测试迁移。** 旧 FtreContext/EventHub/PluginRegistry/PluginLoader 测试替换为 Cordis Context/Fiber/PluginContext/Effect/PluginRuntime 测试，保留相同的依赖缺失、生命周期、事件过滤和失败诊断覆盖。
- [x] **FR4：旧 aggregate API 退役。** `src/ftre/api/routes.py`、`api/app.py` 和旧 setter 测试迁移到 Service-owned Router，删除旧 API 包中不再被引用的实现。
- [x] **FR5：旧 Plugin 公共入口删除。** `ftre.plugin` 包及其旧符号全部删除，防止新代码误用旧入口。
- [x] **FR6：生产导入门禁。** `src/ftre/app`、`platform`、`services`、`features` 和 `cordis` 不得导入 `ftre.plugin`、`ftre.api` 或旧 Kernel/Builtin。
- [x] **FR7：Plugin 入口收敛。** 外部与内置 Plugin 统一使用 `module:attribute` + `apply(ctx, config)` + `PluginContext`；旧 `setup`/`module.Class` 入口明确拒绝。
- [x] **FR8：删除旧实现。** 旧 Kernel、旧 Builtin、旧 aggregate API、旧 `ftre.plugin.api` 和旧外部兼容适配在生产树中删除。

### 2.2 非功能需求

- **回归**：F2 的 HTTP/WS、Session、Tool、Agent 行为保持通过；外部旧插件兼容不在本阶段保证。
- **可审计性**：每个被删除的旧模块必须有新 Owner、迁移测试或兼容说明；禁止无测试裸删。
- **安全性**：删除旧 aggregate API 后，不得恢复模块全局 setter 或配置导入副作用。
- **可回滚**：按 Hook、Builtin、API、Kernel 四个切片独立提交。

## 3. 技术方案

### 3.1 新旧映射

| 旧入口 | 新 Owner |
|---|---|
| `ftre.plugin.kernel.events` | `services/agent/runtime/hooks.py` + `cordis.Context.events` |
| `ftre.plugin.builtin.context_govern` | `plugins/builtin/context_govern/plugin.py` |
| `ftre.plugin.builtin.skill_plugin` | `plugins/builtin/skill/plugin.py` |
| `ftre.plugin.builtin.mcp_plugin` | `plugins/builtin/mcp/plugin.py` |
| `ftre.plugin.builtin.plan_plugin` | `plugins/builtin/plan/plugin.py` |
| `ftre.plugin.builtin.team_plugin` | `plugins/builtin/team/plugin.py` |
| `ftre.plugin.builtin.title_gen` | `plugins/builtin/session_title/plugin.py` |
| `ftre.api.routes` | `services/session/router.py`、`services/agent/router.py`、`services/attachment/router.py`、`plugins/builtin/command/router.py`、Feature Router |
| `ftre.plugin.kernel` | `cordis` + `kernel/plugins` |

### 3.2 测试迁移规则

1. 删除旧测试前，先在 `tests/architecture`、`tests/contracts` 或 `tests/lifecycle` 增加新契约测试。
2. 旧 EventHub 的 `emit/parallel/serial/waterfall/filter` 语义只保留有产品使用价值的 `Context.on/emit/filter` 覆盖；不为自研 API 保留完整复制品。
3. 旧 Builtin 的行为测试改为测试实际 Feature Plugin 的 Service/Tool/Prompt/Router Contribution。
4. HTTP 旧路由测试改为使用 `build_composition()` 的 HttpService Host 或对应 Owner Router。

### 3.3 删除门禁

删除前必须满足：

- `rg "ftre\.plugin\.kernel|ftre\.plugin\.builtin|ftre\.api\.routes|from ftre\.plugin" src/ftre` 无生产引用。
- `python -m pytest -q`、`python -m ruff check src tests` 通过。
- 外部 Plugin synthetic fixture 和 Octo test double 仍能启动/卸载。
- `git diff --check` 通过，删除范围可由迁移提交独立解释。

## 4. 验收标准

- [x] **AC1：Hook 新契约。** Agent Runtime 只从 `services.agent.runtime.hooks` 导入 Hook Context/常量；Cordis Event filter 测试通过。
- [x] **AC2：Feature Plugin 行为保持。** Skill/MCP/Plan/Team/Schedule/ContextGovern/SessionTitle 新 Plugin 的 Tool、Prompt、Router、状态和 cleanup 测试通过。
- [x] **AC3：Kernel 语义覆盖。** 新测试覆盖 Provider/Consumer PENDING→ACTIVE、Effect LIFO、依赖下线、事件 filter 和 required failure。
- [x] **AC4：HTTP 旧表面删除。** `ftre.api.routes`、旧 setter 和 `ftre.api.app` 不再存在；Service-owned Router 路径基线保持。
- [x] **AC5：旧目录删除。** `src/ftre/plugin/kernel`、`src/ftre/plugin/builtin` 和无用 `src/ftre/plugin/api.py` 删除。
- [x] **AC6：生产导入清零。** 新四层和 Cordis 不导入旧 Plugin/API 包；架构测试阻止回归。
- [x] **AC7：入口严格化。** synthetic audit Plugin 使用 `module:attribute` + `apply` 通过；旧 `module.Class`、`setup` 和 `ftre.plugin` 入口被拒绝。
- [x] **AC8：全量质量。** pytest、ruff、diff check、Gateway health、WebSocket attach 和正常 dispose 全部通过。
- [x] **AC9：分支收尾。** F3 所有代码按切片提交，分支干净，执行报告完整。

## 5. 测试计划

- `tests/architecture/test_f3_no_legacy_imports.py`：生产导入清零和旧目录不存在。
- `tests/contracts/test_f3_cordis_hooks.py`：新 Hook Context 与 Cordis filter 语义。
- `tests/lifecycle/test_f3_plugin_cleanup.py`：新 Feature Plugin 的贡献清理和重复 dispose。
- `tests/startup/test_composition.py`：路由、Plugin 状态、Host 启停回归。
- 保留并扩展现有 synthetic Plugin、Octo test double 和 Service contract tests。

## 6. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-21 | 初始定稿；将 F1/F2 的旧 Kernel/API 兼容窗口转为 F3 删除阶段 | 数据面已经完成迁移，可以安全收敛最后一套旧 Plugin 控制面 |
| 2026-08-21 | 完成 Hook/Event 与 SessionTitle 的首批迁移；Cordis Fiber 增加失败回滚 | 先锁定新事件契约和可逆生命周期，再继续清理旧 Builtin 测试与实现 |
| 2026-08-21 | 旧聚合 API 与图片路由测试迁移至 AttachmentService-owned Router | 删除全局 setter 和 aggregate router，保留图片预览兼容行为 |
| 2026-08-21 | 删除旧 `ftre.plugin.kernel`、`ftre.plugin.builtin`、`ftre.plugin.api` 与兼容导出，加入架构导入门禁 | Cordis 与 platform Plugin Runtime 已覆盖依赖、事件、Effect、Loader 语义，旧实现不再需要保留 |
| 2026-08-21 | 完成全量 pytest、ruff、diff、Gateway health、WebSocket attach 与 dispose 验证；F3 标记已验收 | 所有 FR/AC 已有代码、测试和手动证据，进入阶段收尾 |
| 2026-08-21 | 按用户决策删除窄 `ftre.plugin.Plugin`、`LegacyPluginContext`、旧 `setup`/`module.Class` 解析路径；更新 AC7 为严格入口校验 | 不再考虑外部旧插件兼容，确保单一 Cordis `apply` Plugin 契约 |
