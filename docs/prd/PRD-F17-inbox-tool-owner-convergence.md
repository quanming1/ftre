# PRD-F17：Inbox Tool Owner 收敛与 Agent Runtime 去队列透传

| 项目 | 内容 |
|---|---|
| 阶段 | F17 |
| 状态 | 已验收 |
| 范围 | `E:\ftre` 后端与仓内 `packages/ftre-inbox` |
| 外部边界 | 不修改客户端、`E:\ftre-agent-core`、`E:\cordis-py` |
| 目标 | 让 Inbox Service 由 Inbox Plugin 统一拥有队列基础设施，删除 Agent Runtime 中未接通的 `_inbox` 透传 |

## 1. 背景与问题

`TurnExecutor` 预留了 `inbox` 参数并把它复制到 `runtime_context`，但 Agent Provider
创建 `TurnExecutor` 时没有传入该服务，因此 `Injected("inbox")` 实际得到的始终是 `None`。
与此同时，`send_message`、`task` 和 Team Tool 的队列行为仍在通用
`services/tools/builtin` 中注册，形成“Tool 声明依赖 Inbox、Owner 却不拥有 Inbox”的隐藏耦合。
本阶段只解决 Agent Runtime 的死透传和 Inbox 基础队列 Owner；三个 Tool 的业务 Owner
拆分由 F18 完成，不能因为它们调用 Inbox 就把它们归入 Inbox。

## 2. 目标架构

```text
Inbox Plugin
  ├─ provide("inbox")
  ├─ inject("sessions", "agents", "hook_runtime")
  └─ 持有 InboxService、队列 Hook、Worker 与持久化生命周期

F18 业务 Tool Package
  ├─ ftre-messaging：拥有 send_message
  ├─ ftre-task：拥有 task
  └─ ftre-team：拥有 team_* / wait_agent

Agent Plugin / Agent Runtime
  ├─ 只接收 InboundMessage 并执行 Turn
  ├─ 不保存 Queue、Inbox 或 pending 状态
  └─ 不再向 Tool runtime_context 透传 inbox

Tools Plugin
  └─ 只提供 ToolService 注册表和不依赖 Inbox 的基础工具
```

Inbox 作为当前 Gateway 的必选 Plugin 运行；缺失 Inbox Package 时启动必须明确失败，
不创建半成品 Agent Runtime。Inbox 只注册队列 Hook/Worker；业务 Tool 由 F18 三个独立
Package 注册并绑定自己的 Fiber Effect。

## 3. 功能要求

- [x] FR1：`ftre-inbox` Plugin 成为默认 Composition 的必选 Plugin，entry point、required
  和缺失诊断一致。
- [x] FR2：Inbox Plugin 只通过 `ctx.provide("inbox")` 提供队列 Service，并注册队列 Hook、
  Worker 和持久化资源；不拥有消息、任务或团队 Tool。
- [x] FR3：队列相关 Tool 的 Inbox 依赖从 Tool 函数级 `Injected("inbox")` 改为各自业务
  Package Plugin 构造时获得的真实 Service；Tool 不再从 Agent Runtime 查找 Inbox。
- [x] FR4：`services/tools/builtin/__init__.py` 不再注册队列相关 Tool；三个业务 Tool 的
  独立 Owner 和 Package 迁移由 F18 验收。
- [x] FR5：删除 `TurnExecutor.__init__(inbox=...)`、`self._inbox` 和
  `runtime_context["inbox"]`；Agent Runtime 不出现 Inbox/Queue 依赖。
- [x] FR6：`notify`、`invoke`、`task`、Team 投递和等待行为保持现有协议；Inbox 缺失时由
  必选 Plugin 启动诊断阻止 Gateway，而不是在 Agent Runtime 透传一个空句柄。
- [x] FR7：Inbox Plugin unload/restart 能完整撤销和重建队列 Hook、Worker 与持久化句柄；
  业务 Tool 的撤销由 F18 各自 Plugin 负责。
- [x] FR8：更新 Architecture/Contract/Startup/Lifecycle 测试，证明唯一 Owner、无
  `Injected("inbox")` 队列 Tool、Agent Runtime 无 `_inbox`。
- [x] FR9：更新 F12/F14/F16 相关文档中的“可选 Inbox”边界，记录 F17 将 Inbox 改为当前
  Gateway 的必选 Plugin；同步 CHANGELOG 和执行报告。

## 4. 非目标

- 不把 QueueItem、pending、claim 或 Inbox 数据模型搬进 AgentService/Core。
- 不修改 Agent Core Hook 协议，不在 `E:\ftre-agent-core` 增加 Service Locator。
- 不改客户端 wire protocol；已有 queue/status/invoke 行为只做 Owner 迁移。
- 不为本次迁移新增 Port、Coordinator、Facade、Service Bag 或第二套 Tool 执行器。

## 5. 验收标准

- [x] AC1：默认 Composition 中 Inbox 缺失时以 required Plugin 诊断失败；安装后 Inbox ACTIVE。
- [x] AC2：Inbox Plugin 是 `inbox` Service、队列 Hook、Worker 和持久化的唯一 Owner；业务
  Tool 不在 Inbox Plugin 内注册，独立 Owner 拆分见 F18。
- [x] AC3：`rg`/AST 扫描确认 Agent Runtime、Tools Plugin 和基础 Tool 中不存在 Inbox 透传；
  队列 Tool 只由 Inbox Plugin 注册。
- [x] AC4：Inbox 的 admission、claim、steer 和 `agent/before-reasoning` 回归通过；业务
  Tool 行为在 F18 三个 Package 中回归。
- [x] AC5：Inbox unload/restart 后 Hook、Worker 和持久化句柄无重复、无残留，第二次 close 幂等。
- [x] AC6：全量 pytest、architecture/contracts/startup/lifecycle、Ruff、wheel 和 Gateway
  smoke 通过；生成缓存、空目录和临时文件清零。

## 6. 批次计划

1. F17.1：Owner/依赖基线与 PRD/TODO 定稿。
2. F17.2：Inbox Plugin 注册队列相关 Tool，移除通用 Tool 注册。
3. F17.3：删除 Agent Runtime `_inbox` 透传并迁移测试。
4. F17.4：必选 Plugin、缺失诊断、卸载/重启和行为回归。
5. F17.5：全量验收、文档同步、工程卫生和执行报告。

## 7. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-08-24 | 初版：收敛 Inbox Tool Owner，删除 Agent Runtime 死透传 | 审计发现 `TurnExecutor._inbox` 从未由 Agent Provider 接线，`Injected("inbox")` 永远得到 `None` |
| 2026-08-24 | 完成 F17.1–F17.5：必选 Plugin、Inbox 基础队列迁移、Agent Runtime 清理、生命周期/全量测试/wheel/Gateway smoke 通过 | 队列能力回到唯一 Inbox Owner；业务 Tool 的 Package 边界另立 F18 | F17 基础 FR/AC 已验收，原“Inbox 拥有业务 Tool”描述由 F18 纠偏 |
