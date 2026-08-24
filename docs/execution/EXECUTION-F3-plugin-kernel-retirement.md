# F3 旧 Plugin Kernel 与兼容入口退役执行报告

## 1. 执行结论

F3 已完成。旧 Plugin Kernel、旧 Builtin Plugin、旧 aggregate API 和无用
`ftre.plugin.api` 和整个 `ftre.plugin` 包已从生产树删除；Agent Runtime、Feature Plugin
和 HTTP 路由测试全部改用 Cordis/Service-owned 契约。不再保留外部旧式
`setup(ctx, config)`、`module.Class` 或 `LegacyPluginContext` 兼容路径。

本阶段只修改 `E:\ftre`，未修改 Desktop、`ftre-agent-core`、Octo 仓库或客户端。

## 2. 迁移结果

### 2.1 Hook/Event

- `services/agent/runtime/hooks.py` 成为唯一 Hook Context/常量 Owner。
- `TurnExecutor` 直接构造新 `MessagesBuildContext`，不再依赖旧事件 Kernel。
- `cordis.Context` 的 `on`、`filter` 和可逆 disposer 有独立契约测试。
- Plugin apply 中途失败时，Cordis Fiber 以 LIFO 顺序回滚已注册 Effect。

### 2.2 Builtin Plugin

- ContextGovern、Skill、MCP、Plan、Team、Schedule、SessionTitle 的测试改为验证
  `plugins/builtin/*` 或 `services/*` 的真实 Plugin/Service。
- SessionTitle generator 下沉到 `plugins/builtin/session_title/generator.py`，Plugin 入口使用
  `inject = ("sessions", "system_prompt")` 和 `ctx.on/ctx.effect`。
- 启动测试确认七个内置 Feature Plugin 为 `ACTIVE`，并验证 Skills/MCP/Schedule/Teams
  Service 已发布。

### 2.3 HTTP/API

- 删除 `src/ftre/api/routes.py`、`api/app.py`、`api/skill.py` 和 `api/__init__.py`。
- 图片预览测试改用 `AttachmentService` 与 `services/attachment/router.py`。
- 旧全局 setter 和 aggregate Router 不再有生产入口。

### 2.4 Plugin Runtime

- 删除 `src/ftre/plugin/kernel/`、`src/ftre/plugin/builtin/` 和 `src/ftre/plugin/api.py`。
- 新增 `tests/architecture/test_f3_no_legacy_imports.py`，阻止旧命名空间和旧模块回流。
- `ftre.plugin` 包已删除；Cordis 仅接受 `module:attribute` + `apply(ctx, config)` Plugin 入口。
- `LegacyPluginContext`、旧 `setup` 调用和 `module.Class` 解析路径已删除。

## 3. 分阶段提交

| 切片 | 提交 | 内容 |
|---|---|---|
| F3.1 | `6dd430a` | Hook/Event、SessionTitle 迁移与 Fiber 失败回滚 |
| F3.2 | `d372b49` | Builtin Plugin、Kernel 语义和启动测试迁移 |
| F3.3 | `53a04c9` | Aggregate API 删除与 Attachment Router 测试迁移 |
| F3.4 | `b2afc8b` | 旧 Kernel/Builtin/API 删除与架构导入门禁 |
| F3.4 入口收敛 | `3fac399` 后续修订 | 删除外部旧式 Plugin 兼容入口与 setup 适配 |
| F3.5 | `601a48f`、`5416cbc` | 测试格式、文档、验收与执行报告 |

## 4. 验收矩阵

| PRD 项 | 结果 | 证据 |
|---|---|---|
| FR1 / AC1 | 通过 | 新 Hook Owner、Cordis filter 测试、无旧 Kernel 导入 |
| FR2 / AC2 | 通过 | `tests/startup/test_builtin_features.py` 与 Feature/Service 行为测试 |
| FR3 / AC3 | 通过 | `tests/contracts/test_f3_cordis_hooks.py`、`tests/lifecycle/test_f3_plugin_cleanup.py`、Cordis 依赖/Loader 测试 |
| FR4 / AC4 | 通过 | `src/ftre/api/*.py` 删除；`tests/test_image_api.py` 使用 Attachment Router |
| FR5 / AC5 | 通过 | 旧 Kernel/Builtin/api.py 与整个 ftre.plugin 包删除；架构门禁 |
| FR6 / AC6 | 通过 | `tests/architecture/test_f3_no_legacy_imports.py` |
| FR7 / AC7 | 通过 | synthetic Plugin 使用 module:attribute + apply；旧入口拒绝测试 |
| FR8 / AC8 | 通过 | 全量测试、ruff、diff、Gateway/WS 手动验证 |
| AC9 | 通过 | 分片提交、PRD/TODO/CHANGELOG/执行报告已收尾 |

## 5. 自动化验证

```text
python -m pytest -q
351 passed, 1 warning

python -m ruff check src tests
All checks passed!

git diff --check
通过
```

唯一 warning 是既有 `src/ftre/channel/test_channel.py` 中 `TestChannel` 带自定义构造器，
导致 Pytest collection warning；与本阶段无关。

## 6. 手动验证

通过 `start_gateway(config={})` 创建 Composition，并用 FastAPI `TestClient` 验证：

- `GET /api/health` 返回 `200 {"status": "ok"}`；
- 通过 Composition Host 创建 WebSocket Channel，发送 `attach` 帧后收到 `reply_snapshot`；
- 路由总数为 40，HTTP Host 与 WebSocket 复用同一 FastAPI 应用；
- `composition.close()` 正常执行，重复关闭无异常。

## 7. 后续边界

F3 只收敛旧控制面，不重写 F2 已迁移的数据面算法。外部插件必须直接使用
`module:attribute` + `apply` + `PluginContext`；旧插件兼容不属于系统运行边界。

## 8. 收尾状态

- 分支：`feature/F3-plugin-kernel-retirement`
- PRD：`docs/prd/PRD-F3-plugin-kernel-retirement.md`（已验收）
- TODO：阶段 F3（`done`）
- CHANGELOG：已追加 `[未发布]` F3 条目
- 最终工作区：应保持干净
