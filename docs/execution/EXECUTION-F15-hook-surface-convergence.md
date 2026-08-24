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
| `agent/error`、`agent/session-start`、`agent/status` 无发布点 | `src/ftre/services/agent/hooks.py` 定义与导出；AST/rg 无生产 dispatch | F15.3 | 待清理 |
| `agent/request` 名称与 AgentConfig 转换语义不符且无生产消费者 | `AgentRequestPayload`/`AGENT_REQUEST_SPEC`；运行链仅由 TurnExecutor 旧配置阶段持有 | F15.3 | 待清理 |
| `agent/turn-stopped` 与 after-turn 重叠且无生产消费者 | `TurnStoppedPayload`/Spec；无生产 listener | F15.3 | 待清理 |
| `session/event` 与 MessageBus 事实通知重复 | `SESSION_EVENT_SPEC` 与 `services/session/events.py` 发布链 | F15.4 | 待清理 |
| `session/flush` 无消费者、不是实际多 Store 屏障 | `SESSION_FLUSH_SPEC`；无生产 listener | F15.4 | 待清理 |
| Inbox 三个细粒度 mutation Hook 无消费者 | `ftre_inbox/hooks.py` 与 Package dispatch 搜索 | F15.4 | 待清理 |
| `messaging/inbound` 名称表达不出控制路由 | `MESSAGING_INBOUND_SPEC` 被 Bus 消费链使用 | F15.3 | 待改名 |
| EMIT 异步 listener detached，当前承载关键清理/权威状态 | `HookRuntime.dispatch` EMIT 分支；Session/Inbox/WebSocket listener | F15.2/F15.4 | 待修复 |
| 注册 API 同时存在 Context Effect、Runtime companion 和 Plugin 手工 receipt Effect | `HookRuntime.register` 与 Package/Plugin 注册点 | F15.2/F15.5 | 待收敛 |

## 命令证据

| 命令 | 结果 |
|---|---|
| `python -m pytest -q tests/architecture/test_f15_hook_surface.py` | 32 passed |
| `git diff --check` | 通过 |
| `python -m ruff check --no-cache src tests packages` | F15.1 收尾时重跑并记录 |

## 后续批次输入

- F15.2 必须先解决 EMIT/awaited、Context 必填和唯一 Effect Owner，再迁移消费者。
- F15.3 只能删除 Host Agent/Messaging 旧名；不得修改 Core 7 个 Hook。
- F15.4 需要用 Barrier/Event 证明 Session dispose、Inbox revision 和 WebSocket 状态顺序。
- F15.5 需要保留 `ftre-compaction` 的独立包边界，并处理执行前已有的注释改动而不覆盖它。

