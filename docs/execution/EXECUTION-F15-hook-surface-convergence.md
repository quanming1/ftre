# F15 执行报告：Hook 语义收敛与生命周期安全

## 运行信息

| 项目 | 值 |
|---|---|
| 阶段 | F15 |
| PRD | `docs/prd/PRD-F15-hook-surface-convergence.md` |
| 当前范围 | Host-only：全系统 29→17；Core 7 项冻结 |
| 分支 | `feature/F15-hook-surface-convergence` |
| 开始日期 | 2026-08-24 |
| 状态 | 开发中 |

本报告只记录实际代码、命令和测试证据。没有证据的 FR/AC 不勾选；执行前已经存在的
非 F15 修改不纳入本阶段提交。

## F15.1 基线

### Hook Owner 与定义证据

| Owner | 当前数量 | 定义/导出证据 | 处理计划 |
|---|---:|---|---|
| Agent Core | 7 | `ftre_agent_core/hooks.py` 的 Tool 4、LLM 1、Agent 2；ftre 通过 `services/tools/hooks.py`、`services/llm/hooks.py` 和 `services/agent/hooks.py` 重导出 | F15 冻结；后续 F16/Core C3 再评估 |
| Agent Host | 10 | `src/ftre/services/agent/hooks.py` 的 before/after/request/error 与 lifecycle Spec | F15.3 收敛为 before-run/after-run/run-error |
| Session | 4 | `src/ftre/services/session/hooks.py` | F15.4 保留 created/disposed |
| Messaging | 1 | `src/ftre/services/messaging/bus/ingress.py` | F15.3 改为 messaging/route |
| System Prompt | 1 | `src/ftre/services/system_prompt/hooks.py` | F15 保留 |
| Inbox Package | 6 | `packages/ftre-inbox/src/ftre_inbox/hooks.py` | F15.4 收敛为 before-claim/changed/status-changed |

唯一名称共 29 个。基线扫描器位于
`tests/architecture/test_f15_hook_surface.py`，从实际模块公开的 `*_SPEC` 读取，不复制
生产 Hook 对象；Host 对 Core Spec 的重导出按唯一名称计数，避免把导出面误算成第二 Owner。

### F15 目标

目标集合共 17 个：Core 冻结 7 个，Host/Package 目标 10 个。目标集合和 Core 边界均由
`test_f15_target_set_is_explicit_and_core_boundary_is_frozen` 断言；迁移完成前不得把目标
断言改成宽泛前缀或 allowlist。

### 当前债务基线

| 债务 | 证据 | 清理批次 | 当前状态 |
|---|---|---|---|
| `agent/error`、`agent/session-start`、`agent/status` 无发布点 | `src/ftre/services/agent/hooks.py` 定义与导出；AST/rg 无生产 dispatch | F15.3 | 已删除 |
| `agent/request` 名称与 AgentConfig 转换语义不符且无生产消费者 | `AgentRequestPayload`/`AGENT_REQUEST_SPEC`；运行链仅由 TurnExecutor 旧配置阶段持有 | F15.3 | 已删除 |
| `agent/turn-stopped` 与 after-turn 重叠且无生产消费者 | `TurnStoppedPayload`/Spec；无生产 listener | F15.3 | 已删除 |
| `session/event` 与 MessageBus 事实通知重复 | `SESSION_EVENT_SPEC` 与 `services/session/events.py` 发布链 | F15.4 | 已删除 |
| `session/flush` 无消费者、不是实际多 Store 屏障 | `SESSION_FLUSH_SPEC`；无生产 listener | F15.4 | 已删除 |
| Inbox 三个细粒度 mutation Hook 无消费者 | `ftre_inbox/hooks.py` 与 Package dispatch 搜索 | F15.4 | 已删除 |
| `messaging/inbound` 名称表达不出控制路由 | `MESSAGING_INBOUND_SPEC` 被 Bus 消费链使用 | F15.3 | 已改名 |
| EMIT 异步 listener detached，当前承载关键清理/权威状态 | `HookRuntime.dispatch` EMIT 分支；Session/Inbox/WebSocket listener | F15.2/F15.4 | 已修复：关键 Hook 改 awaited PARALLEL |
| 注册 API 同时存在 Context Effect、Runtime companion 和 Plugin 手工 receipt Effect | `HookRuntime.register` 与 Package/Plugin 注册点 | F15.2/F15.5 | 已收敛 |

## 命令证据

| 命令 | 结果 |
|---|---|
| `python -m pytest -q tests/architecture/test_f15_hook_surface.py` | 32 passed |
| `git diff --check` | 通过 |
| `python -m pytest -q tests/architecture tests/contracts tests/startup` | 160 passed |
| `python -m pytest -q tests/hooks tests/contracts packages/ftre-inbox/tests packages/ftre-compaction/tests` | 100 passed |
| `python -m ruff check --no-cache src tests packages` | 通过 |

## F15.8/F15.9 最终输入

- 生产源码、Package 和当前架构测试中已无删除的 Host Hook 名称；历史 PRD/执行记录中的旧名
  仅作为迁移证据保留，不属于当前运行契约。
- 仍需在 F15.9 记录执行前遗留修改隔离、CHANGELOG、全量命令、Gateway smoke 和外部 CI 状态。

## F15.2 / F15.3 结果

- F15.2：Runtime 注册参数改为 `all_agent_scopes`，诊断 scope 从 Context/策略推导；生产
  Plugin 注册显式传入 Context；receipt 的生命周期由 Runtime Fiber Effect 单独管理。
- F15.3：Agent Host 10 项收敛为 `agent/before-run`、`agent/after-run`、`agent/run-error`；
  删除 Agent lifecycle/request/turn-stopped Spec 及 dispatch；`messaging/inbound` 改为
  `messaging/route`，Command 与 Inbox 保持原有路由顺序。
- F15.3 后唯一 Hook 快照为 22 项（Core 7 + Agent 3 + Session 4 + Messaging 1 + Prompt 1 + Inbox 6）。
- 证据：F15 Hook/契约/Package 专项 91 passed，ruff 通过；Session/Inbox awaited 和三种 mutation
  Hook 仍属于 F15.4。

## F15.4 结果

- Session 只保留 `session/created`、`session/disposed`，两者改为 awaited PARALLEL/OBSERVE；
  删除 `session/event`、`session/flush` 及其 DTO、Service flush 入口和重复事件通知。
- Inbox 只保留 `inbox/before-claim`、`inbox/changed`、`inbox/status-changed`；changed/status
  改为 awaited PARALLEL，删除 inserted/claimed/discarded 及统一 mutation wrapper。
- WebSocket 继续通过 Inbox 的公开 HookSpec 读取权威 snapshot/status，wire contract 不变。
- 目标 Hook 快照已精确为 17 项；F15.4/生命周期/架构/启动/契约专项 79 passed，ruff 通过。

## F15.5 结果

- `ftre-inbox` 监听 `messaging/route`、Core `agent/before-reasoning`、awaited
  `session/disposed`，发布 before-claim/changed/status-changed；无旧 mutation Hook。
- `ftre-compaction` 监听 `agent/after-run`、`agent/run-error`、`inbox/before-claim`，压缩
  行为和可选卸载语义保持；Command、WebSocket、Session Title 均使用公开目标 Spec。
- 生产扫描无旧 Host Hook、`global_listener`、receipt 二次 Effect 或 Package 私有跨 Owner import。
- 证据：Package、启动、生命周期专项 85 passed；架构/契约专项 141 passed；ruff 通过。

## F15.6 / F15.7 结果

- 新增 `tests/lifecycle/test_f15_faults.py`：验证 awaited Session dispose listener、in-flight
  unload drain 和 OBSERVE 失败脱敏；与既有 Inbox 取消、恢复、pending 去重测试共同覆盖故障矩阵。
- 证据：F15 生命周期新增 2 passed；此前 lifecycle/startup/Package 85 passed、architecture/contracts
  141 passed；ruff 通过。
- Package wheel 已在 repo 外构建：
  - `ftre_inbox-0.1.0-py3-none-any.whl`，SHA256
    `2F9CCDF5268D7F57E8BB7AD28F538601795B39BDE4C180FE9D034BA7C899BF03`；
  - `ftre_compaction-0.1.0-py3-none-any.whl`，SHA256
    `CF735F6A105815DD189871FC25A749C3BD310F8126227D6B79CC42EED9DE086E`。
- 两个 wheel 已安装到 `E:\tmp\ftre-f15-clean-site` 并从该路径成功 import；wheel 清单无
  `__pycache__`、`.pyc`、tests 或 Host 私有源码。Core 7 项名称、DTO 和依赖未修改。
- 构建生成的 package `build/` 与 `egg-info/` 已移出仓库至
  `E:\tmp\ftre-f15-generated-trash`，工作区不保留生成物。

## F15.8/F15.9 当前结果

- 全量 `python -m pytest -q`：486 passed；`python -m ruff check --no-cache src tests packages`：通过；
  `git diff --check`：通过。
- 真实 Gateway smoke：`python -m ftre.main gateway --foreground --port 48659 --host 127.0.0.1`
  启动成功；`GET /api/health` 返回 HTTP 200 `{"status":"ok"}`；真实 WebSocket 连接建立并正常
  关闭；Ctrl+C 优雅关闭 Channel、Command、Schedule、MCP 和 Plugin 资源。
- 生成缓存 `__pycache__`/`.pyc`、root `build`、`.pytest_cache` 已移出工作区到
  `E:\tmp\ftre-f15-cache-trash`；仓库内缓存扫描为零。
- 当前生产源码、Package 和架构扫描均无删除的 Host Hook 名、`global_listener` 或重复
  `receipt.dispose`；F15 目标快照为 17 项。
- F15 GitHub Actions 尚未触发（当前 feature 分支未 push）；因此 AC19 和最终“已验收/发布”
  仍保持未完成，不能把本地绿色结果冒充远程 CI 证据。
