# 架构清理审计执行报告

## F7/C1 复审附录（2026-08-21）

本附录是对上文 F1-F6 清理记录的当前状态复核，范围扩展为经用户授权协同验证的
`E:\ftre` 与 `E:\ftre-agent-core` 两个独立仓库；客户端、`E:\cordis-py` 源仓库和用户
运行数据目录不在修改范围。

### Owner 与旧实现核对

| 旧 Owner/入口 | 当前 Owner | 删除/迁移证据 |
|---|---|---|
| Core `FtreCoreHookManager`、`ON_*` | Core `HookDispatcher` + typed `HookSpec` | Core 生产源码 AST 无 ftre/Cordis 反向依赖；旧符号扫描无命中 |
| ftre `ToolHookBridge` / `HookedToolRegistry` | Core `ToolHandler` 直接 dispatch Tool contracts | `src/ftre/infrastructure/agent_core` 不存在；ftre `src` 无桥接引用 |
| ftre `HookedLLMAdapter` | Core `ReasoningExecutor` 直接 dispatch `llm/stream` | 适配器文件和导出删除；Core direct pipeline 测试通过 |
| finalize 后的 stop observation | Core finalize 前 `StopTurn`/`ContinueTurn`，ftre finalize 后 `agent/turn-stopped` | continuation budget、取消和 stop/continue 测试通过 |

### 生命周期复审

| 资源 | 创建/注册 Owner | 停止/回滚证据 |
|---|---|---|
| Cordis Plugin/Fiber | `Composition → PluginManager → PluginLoader` | Fiber dispose/restart、失败回滚和 required/optional 诊断专项通过 |
| Hook Listener | `HookRuntime.register` + `ctx.effect` | unload/restart、in-flight drain、scope 隔离专项通过 |
| AgentLoop/Lane | `AgentLoopProvider` / `SessionLaneRegistry` | stop admission→Lane→compaction→scope；重复 start/stop 语义已补门禁 |
| ChannelManager | `bootstrap` 真实 Host | 重复 start 不重复创建 dispatch task，重复 stop 不重复停止 Channel；新增生命周期回归 |
| Schedule/MCP/Compaction Task | 各 Feature Plugin | `ctx.effect` 绑定 stop/close；既有 schedule、MCP、compaction 生命周期测试通过 |

### 当前最终证据

```text
E:\ftre-agent-core: python -m pytest -q       → 234 passed
E:\ftre:            python -m pytest -q       → 389 passed（含本次 ChannelManager 回归）
两仓库 ruff check（Core .；ftre src tests）   → All checks passed!
两仓库 git diff --check                       → 通过
Core 外部依赖 AST 检查                        → 无 ftre/Cordis 反向依赖
ftre 旧路径 AST 检查                           → 无 ftre.plugin/ftre.agent 等旧 import
Gateway smoke                                    → GATEWAY START OK / GATEWAY CLOSE OK
缓存复核                                        → 两仓库 __pycache__/.pytest_cache/.ruff_cache 均为 0
源码/测试空目录复核                              → 两仓库 src/tests 均为 0
```

活动规范已同步：Core `AGENTS.md`、两仓库 TODO、F7/C1 PRD 的状态/日期/变更记录和
两仓库 CHANGELOG 均已更新。历史 PRD、superpowers 设计稿和已跟踪的 `trace.json` 是
回溯资料，不属于运行入口；用户 `.ftre/` 配置、`data/` 日志和数据库未删除。

## 1. 范围与结论

本轮使用 `refactor-cleanup-audit` 对 `E:\ftre` 及经用户明确授权的
`E:\ftre-agent-core` 协同改动做收尾清理。目标是移除已经没有生产、测试或动态入口
引用的死代码、迁移兼容壳和重复 HTTP 注册 API，并修正文档与当前代码树不一致的问题；
不改变客户端协议、`E:\cordis-py` 源仓库或用户运行数据。

清理结果：已删除 13 个无引用/无效模块或资源，移除 2 个仅为旧 HTTP 聚合 API 服务的转发方法，
同步当前架构说明，并保留仍承担持久化数据兼容或隔离测试职责的 fallback。最终质量
门禁见第 5 节。

## 2. 审计方法

1. 读取仓库协作约束、PRD/TODO、提交规范和本 Skill 的完整流程。
2. 建立生产代码、测试、Composition manifest、动态 `module:attribute` 入口和文档引用
   的交叉检索基线。
3. 对每个删除候选执行反向引用核验；仅当 `src`、`tests`、manifest 和运行时字符串均
   无引用时删除。
4. 删除后先运行架构、启动、生命周期和 Schedule 专项，再运行全量门禁。
5. 最后清理测试/构建产生的缓存，并复核空目录、临时脚本、敏感调试文件和工作区状态。

## 3. 已删除的无引用代码

| 路径 | 结论 |
|---|---|
| `src/ftre/app/cli/logging.py` | 仅转发 `ColorFormatter`，CLI 直接使用 `ftre.main` 实现 |
| `src/ftre/app/gateway/diagnostics.py` | `StartupDiagnostics` 无生产、测试或动态入口引用 |
| `src/ftre/app/gateway/http/health.py` | 健康路由由 `HttpService.register_health()` 唯一拥有 |
| `src/ftre/app/gateway/http/server.py` | 未被 App、Composition 或测试引用；监听生命周期由外部 Gateway Host 管理 |
| `src/ftre/app/gateway/http/server_plugin.py` | 无 manifest/入口引用，属于未启用的旧 Server Plugin |
| `src/ftre/app/gateway/http/service_plugin.py` | 默认清单已使用 `services.http.plugin:apply`，该文件只是旧入口 |
| `src/ftre/features/mcp/private.py` | `private_scope` 无调用方；MCP 私有配置由 Feature Service 处理 |
| `src/ftre/features/skill/store.py` | 仅重导出 `SkillService`，真实 Owner 是 `features/skill/service.py` |
| `src/ftre/services/agent/events.py` | `AgentLifecycleEvent` 无调用方，语义 Hook 已由 `services/agent/hooks.py` 提供 |
| `src/ftre/services/session/compat.py` | `SessionManager` 兼容别名无调用方，旧 Session 入口已退役 |
| `src/ftre/services/system_prompt/base.md` | 只有 HTML 注释，却会被注册进 Prompt；应用基座实际由 `services/agent/config.py` 加载 |
| `src/ftre/services/config/models.py` | `ConfigValue` 无消费者、未公开导出 |
| `src/ftre/services/session/title/config.py` | `TitleConfig` 无消费者；真实配置模型是 `generator.py` 的 `TitleGenConfig` |

同时删除：

- `PluginManager.routers` 旧只读视图；Host 统一消费 `HttpService` 注册表。
- `HttpService.router_objects()` 旧聚合方法；没有正式调用方，避免再次暴露第二套路由 Owner。

## 4. 明确保留的代码

以下命中 `legacy`/`fallback` 的内容经过审计后保留，因为它们不是死代码：

- Session/Trace/JSON 数据格式的读取迁移逻辑，负责已有用户数据恢复。
- `WebSocketChannel` 的隔离测试 Bus fallback，只在未提供完整 Durable Service 的测试场景使用。
- 工具、MCP、附件和进程管理中的异常边界与系统 PATH fallback，属于运行时容错而非模块兼容壳。
- `features/schedule/channel.py`、`store.py` 和 `tool.py`，分别承担 Cron Channel、持久化 Store
  和 Tool factory，均由 `features/schedule/plugin.py` 的动态能力组合使用。

### 4.1 已核验但不在本轮删除范围的债务

| 位置 | 证据 | 处理结论 |
|---|---|---|
| `services/messaging/channel/providers/{websocket,subagent}/plugin.py` | 没有默认 manifest 引用；`bootstrap.py` 在真实 Gateway 路径手工构造 Channel | 不是无引用安全删除项，保留并列为后续“Channel Provider 单一入口”重构 |
| `services/messaging/channel/providers/websocket/channel.py` | 真实负责 WS 协议、连接 attach、快照和附件校验，623 行 | 真实 Owner，不作为死代码删除；后续可拆为协议/连接/附件适配子模块 |
| `services/session/service.py`、`persistence/repository.py` | 承担 Session CRUD、Mailbox admission、持久化和生命周期，均被 Runtime/Router 使用 | 真实数据面 Owner；后续按职责拆分，不做机械切文件 |
| `services/tools/builtin/team.py` | Team 工具拥有多条用户可调用行为和 Agent/Session 注入 | 真实行为 Owner；后续可按 Tool family 拆分 |
| `src/ftre/system_prompt.md` | 被 `services/agent/config.py` 唯一加载，内容是实际系统提示词 | 保留；与已删除的无效 `services/system_prompt/base.md` 不是同一资源 |

## 5. 文档与工程卫生

- `AGENTS.md`：移除不存在的本地 `src/cordis` 和 `PluginContext` 描述，改为官方 `cordis.Context`
  与 `services/agent_loop/provider.py`。
- `README.md`：移除“旧包暂时保留”和不存在的 `factory.py`/`tests/plugin` 描述，说明显式插件
  manifest 边界。
- `services/system_prompt/plugin.py`：移除无效 `base.md` 资源加载；Session title Plugin 注释同步到结构化 Prompt Hook。
- `docs/TODO.yaml`：F2/F3/F6 模块路径同步到当前 Owner，修正 F6.11/F6.12 顺序。
- 删除并复核临时调试脚本；不删除用户运行数据目录 `.ftre/`、`data/`。
- 测试夹具 `PausingAgent` 删除未使用的 `reply_id_holder` 参数；保留 async-generator 测试所必需的不可达 `yield`。
- Channel 注释中的旧 `ftre.bus.protocol` 路径已改为当前 `ftre.services.messaging.bus.protocol`。

## 6. 验证记录

删除后专项验证：

```text
python -m pytest -q tests/architecture tests/startup tests/lifecycle tests/features/schedule
69 passed
python -m ruff check --no-cache src tests
All checks passed!
```

孤立资源清理专项：

```text
python -m pytest -q tests/architecture/test_cleanup_no_orphan_resources.py tests/contracts/test_f1_services.py tests/contracts/test_f7_hook_pipeline.py tests/startup/test_composition.py
13 passed
```

最终全量门禁与 Gateway smoke：

```text
python -m pytest -q
385 passed in 45.89s

python -m ruff check --no-cache src tests
All checks passed!

python -m vulture src/ftre --min-confidence 90
无高置信度死代码

python -c "yaml.safe_load(docs/TODO.yaml)"
TODO YAML OK

git diff --check
通过

Gateway smoke: GATEWAY START OK / GATEWAY CLOSE OK

生产源码 stale-reference scan: clean
python -m vulture src/ftre --min-confidence 90: 无高置信度死代码
```

最终生成物复核：`src`/`tests` 下无 `__pycache__` 或 `.pyc`，无空目录，无 `.tmp_*`
调试脚本；`build/`、`.pytest_cache/`、`.ruff_cache/` 和 `src/ftre.egg-info/` 已清理。

## 7. 后置项

- F6.12（cordis-py PyPI 发行物和脱离 `E:\cordis-py` 的洁净安装）仍按 PRD 保持 todo。
- 本报告不执行 commit、push、merge 或跨仓库修改；保留用户当前分支的累计改动供后续分批提交。
