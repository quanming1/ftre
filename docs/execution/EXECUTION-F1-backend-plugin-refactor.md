# F1 后端插件化重构总执行报告

> 基座纠偏说明（F6，2026-08-21）：本报告记录的是 F1 当时的历史执行结果。F1 报告中提到的 `src/cordis/` fallback 已被审计确认不是真正的 `cordis-py` 运行时，现已删除；当前 ftre 使用 `E:\cordis-py` 提供的官方 `cordis-py==0.4.0`。当前基座、安装方式和验证结果以 `docs/prd/PRD-F6-semantic-hook-system.md` 为准。

## 1. 执行结论

F1 PRD 已在 `feature/F1-backend-plugin-refactor` 分支完成开发和验证。所有改动限定在 `E:\ftre`，没有修改 Desktop 客户端、`ftre-agent-core`、Octo 独立仓库或其他项目。

分支当前已按批次提交，工作区干净。F1 的新启动路径由 `main.py → app.gateway.bootstrap → Composition → PluginManager → cordis.Context` 负责；旧 `ftre.plugin.kernel` 只保留迁移兼容 API，不再被新四层目录或默认 Composition 作为运行时内核使用。

## 2. 交付内容

### 2.1 运行时与装配

- `src/cordis/`：提供 ftre 使用的 `Context`、`Fiber`、`FiberState`、`Service`、`Inject`、`Effect`，支持 PENDING/ACTIVE、依赖上线/下线重载、LIFO Effect 清理和幂等 dispose。
- `src/ftre/platform/plugin_runtime/`：Manifest、Catalog、Discovery、Loader、Manager、诊断和 required failure 处理。
- `src/ftre/app/gateway/composition.py`：唯一默认 Composition 清单和启动期路由装配。
- `src/ftre/app/gateway/bootstrap.py`：Gateway 真实运行时入口；AgentLoop、SessionLane、MessageBus 和 WebSocket 通过公开 Service 接入。
- `src/ftre/main.py`：收敛为日志、Typer 参数和 GatewayRuntime 转发，不再直接构造业务运行时。

### 2.2 Service 和 Feature 边界

新增并测试了以下公共能力：

| 层 | 能力 |
|---|---|
| `services` | Config、Filesystem、Workspace、HTTP、SystemPrompt、MessageBus、Channel、Session、Agent/Profile、Tool、Command、Attachment、Trace |
| `features` | Skill、MCP、Plan、Team、Schedule、ContextGovern、SessionTitle |
| `app` | CLI、Composition、FastAPI Host、uvicorn Server |
| `platform` | Cordis/Plugin Runtime 项目级适配 |

关键行为：

- ConfigService revision、expected-revision、原子写和 watcher。
- FilesystemService 的统一路径 policy、大小限制和原子写。
- HttpService 的 route contribution、owner、冲突检查、freeze 和 `restart_required`。
- ToolService 的 global/agent scope、shadow、allow/deny、schema provenance。
- SystemPromptService 的有序 Section 与 Assembly Receipt。
- 外部 Plugin 只有显式启用后才 import；兼容 `module.Class`，规范入口为 `module:attribute`。
- Octo name-only 历史配置可解析到独立仓库 shim；旧 `setup(ctx, config)` Plugin 通过明确的 LegacyPluginContext 适配，不污染新 API。

### 2.3 测试与文档

- 新增 `tests/architecture/`、`tests/contracts/`、`tests/lifecycle/`、`tests/startup/`。
- 新增 synthetic third-party audit fixture，只依赖公开 Service，不复制外部审计仓库代码。
- 新增路由兼容快照，覆盖 `/api/traces`、`/api/sessions`、`/api/config`、`/api/cron`、`/api/commands`、`/api/images`、`/api/agents`、`/api/skills`、`/api/mcp` 和 WebSocket `/`。
- 新增本执行报告和 `[未发布]` CHANGELOG 条目。

## 3. 分阶段结果

| 阶段 | 结果 | 对应提交 |
|---|---|---|
| F1.1 | Cordis 基座、四层目录、Manifest/Composition 骨架 | `ecfe507` |
| F1.2 | Config/Filesystem/HTTP/路由 Registry | `7679275` |
| F1.3 | Session/Agent/Bus/Channel 接入真实 Gateway | `b759595` |
| F1.4 | scoped Tool/Skill/Prompt/MCP/Plan/Team/Schedule Plugin | `81a92fd` |
| F1.5 | 外部 Plugin、Octo 兼容和 synthetic audit fixture | `5c96844`, `7df16ea` |
| F1.6 | 全量 lint、生命周期/导入边界、手动 Gateway、收尾文档 | 本报告对应最终提交 |

## 4. 验证结果

### 自动化

```text
python -m pytest -q
366 passed, 1 warning

python -m ruff check src tests
All checks passed!
```

唯一 warning 是既有 `src/ftre/channel/test_channel.py` 中 `TestChannel` 带自定义构造器导致的 Pytest collection warning；没有新增测试失败或 warning。

### 手动 Gateway

在禁用外部 Octo 网络通道的 test-double Composition 下：

- `GET /api/health` 返回 `200 {"status":"ok"}`。
- WebSocket `/` 可以建立连接并完成 attach snapshot。
- HTTP 创建 Session 成功。
- WebSocket `user_message` 获得 admission ACK。
- WebSocket cancel 获得 control ACK，任务取消后根 Composition 可清理。
- Gateway task 取消后 Session、Channel、Cron、Plugin Effect 清理完成。

在当前用户配置下，Octo Plugin 可进入 `ACTIVE`；本机外部 Node bridge 因运行环境依赖退出，但未阻止 Gateway health 和基础 WebSocket，属于外部网络/运行时依赖问题，不是 F1 Composition 失败。

### 质量收尾

- 没有使用全局 ruff ignore；存量异常边界、依赖注入默认值和清理容错均使用行级、带 F1 说明的审查标记。
- `git diff --check` 通过。
- 全量源代码已通过 `compileall` 和 pytest 导入。

## 5. 未包含内容

- 未修改 `E:\binn\ftre-desktop`。
- 未修改 `E:\ftre-agent-core`。
- 未移动或重写 `C:\Users\蒋全明\.ftre\plugins\octo_plugin`。
- 未实现 Desktop Client Extension、Plugin Marketplace、进程隔离、HMR、完整 Recovery/Safe Mode；这些仍在 PRD 非目标或后续 TODO 中。

## 6. 分支收尾

- 分支：`feature/F1-backend-plugin-refactor`
- 基线：`develop`
- 提交方式：分阶段提交，未 push、未创建 PR、未合并 develop。
- 最终状态：`git status --short --branch` 仅显示当前分支，无未提交代码。
