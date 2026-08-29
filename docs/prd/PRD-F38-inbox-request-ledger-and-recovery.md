# PRD-F38 Inbox 重复投递与恢复幂等修复

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F38 |
| 名称 | Inbox 重复投递、终态消费与恢复幂等修复 |
| 状态 | 开发中 |
| 创建日期 | 2026-08-29 |
| 定稿日期 | 2026-08-29 |
| 验收日期 | — |
| 关联文档 | `docs/TODO.yaml` F38；`docs/prd/PRD-F35-agent-service-inbox-message-boundary.md`；`docs/prd/PRD-F23-steering-message-boundary.md`；`docs/prd/PRD-F24-queue-operation-response.md`；`AGENTS.md` |

> 本版本是对原 F38 草案的范围收缩。当前阶段只修复已复现的重复投递、错误消费、消息时间覆盖、运行幂等和存储路径问题；不提前引入独立 RequestLedger、SQLite 或新的调度器。

## 1. 背景与目标

### 1.1 已复现故障

在 `ws_sess_cb562d843719` 中观察到：

1. 请求 A“把客户端 dev 跑起来我看看”已经被 claim 并执行。
2. 用户取消 A 后，Inbox 对 `cancelled/interrupted` 调用 `repository.release()`，A 从 `inflight` 返回 `pending`。
3. 用户随后发送请求 B，worker 按 FIFO 再次执行 A，B 被旧请求阻塞。
4. A 重投时，`emit_user_message_if_absent()` 重新生成事件时间，覆盖了已有 UserMessage 的 `created_at`；刷新后用户消息与 Assistant 的时间顺序异常。
5. Inbox 监听 `idle/cancelled/failed` 并唤醒 worker；`idle` 没有说明是正常完成还是取消。
6. Inbox 找不到公开的 `SessionService.sessions_root()` 时回退到 `Path.cwd()/.ftre-inbox`，发行包把队列写入安装目录。
7. 已发布运行包中的 Assistant metadata 缺少 `request_id/run_id`，Session 无法可靠识别同一请求的既有 Run。

### 1.2 本阶段目标

在不重写 Inbox 架构的前提下，建立以下不变量：

```text
Agent 已开始执行的请求永不自动回到 pending。
普通队列只在正常 RunCompleted 后自动消费。
相同 request_id 不重复创建 UserMessage 或 Agent Run。
已有 UserMessage 的身份、时间和内容不可被重放覆盖。
Inbox 不写入安装目录，工作区与发行包使用同一个用户数据根。
```

### 1.3 非目标

- 不新增独立 `RequestLedger` Service，不迁移到 SQLite。
- 不新增第二套 Inbox、Queue、Agent 状态机或 Coordinator。
- 不修改 Agent ReAct 算法、LLM Retry/Fallback、ToolService 或 Core Hook 协议。
- 不改变客户端布局；仅配套增加 queue/status/reconnect 回归验证。
- 不改变 Steer 的既有产品语义；只防止 Steer 错误地作用于后续 Run。
- 不通过 kill/restart 正在运行的 Gateway 完成验证。

## 2. 当前实现与最小修复边界

### 2.1 当前 Owner 保持不变

```text
InboxRepository  -> Inbox 持久化、claim、ack、release、snapshot
InboxService     -> admission、worker、AgentService 调用、队列 Hook
AgentService     -> Agent Run、状态和 request/run 幂等
SessionService   -> UserMessage/Assistant 持久化与 Projection
WebSocket        -> queue/status/event wire 协议
客户端           -> 服务端快照和事件投影
```

本阶段不重新划分 Package，只修正这些 Owner 之间的状态交接。

### 2.2 `release()` 的最小正确语义

```text
PENDING --claim--> INFLIGHT
INFLIGHT --Agent 未开始/准备阶段失败--> PENDING  (允许 release)
INFLIGHT --UserMessage 已写入或 Agent 已开始--> TERMINAL/INTERRUPTED (禁止 release)
```

`repository.release()` 不是“取消后重试” API，而是“尚未交付成功时归还队列” API。取消后的显式重试必须由上层产生新的 request/attempt，不能把原 QueueItem 静默放回队首。

### 2.3 不用 `idle` 作为消费触发器

`idle` 是状态快照，可能来自正常完成、取消、异常和恢复。Inbox 继续接收 Agent 状态用于 UI，但普通 worker 只允许由明确的成功完成信号唤醒：

```text
RunCompleted  -> 清除冻结 -> 消费下一条 next-turn
RunCancelled  -> 冻结队列，不消费
RunFailed     -> 冻结队列，不消费
RunInterrupted-> 冻结队列，不消费
RunPaused     -> 冻结队列，不消费
```

如果当前 Agent API 只能提供 `idle`，必须同时提供 `idle_reason=completed|cancelled|failed|interrupted|paused`；没有原因的 idle 不得触发消费。

## 3. 功能需求

### FR1：执行后请求不可重新入队

- [ ] A 在 Agent 尚未开始前失败，可以释放回 pending。
- [ ] A 已写入 UserMessage、已产生 ReplyStart、已执行 Tool 或已产生 Assistant 输出后，取消/失败/中断都不得调用 `repository.release()`。
- [ ] 终态请求保留 request_id/run_id 和 terminal reason，不能被普通 worker 再次 claim。
- [ ] 旧 inflight 在进程关闭时不得无条件回排；恢复必须依赖 Session/Assistant 证据。

### FR2：普通队列只在正常完成后消费

- [ ] 新会话首次显式发送仍可启动第一条消息。
- [ ] active Run 期间新增普通消息只进入 pending。
- [ ] `RunCompleted` 后最多消费一条队首 next-turn；消费完成后由下一个明确的完成事件继续推进。
- [ ] cancel、pause、failed、interrupted、Gateway 恢复和普通 idle 不得自动发送 pending。
- [ ] 队列冻结时新消息保留，等待显式 resume/start，不得被旧消息抢占。

### FR3：UserMessage 真正幂等

- [ ] 通过 `session_id + request_id` 生成稳定 UserMessage/Event ID。
- [ ] 已有消息命中时返回原 message_id、原 created_at、原 content、原 metadata，不重新覆盖。
- [ ] 相同 request_id 但 content/attachments 不同返回 `request-content-conflict`。
- [ ] 重放不改变 Session 数组顺序，不制造第二条用户消息。

### FR4：Agent Run 幂等

- [ ] AgentService 的 `run()` 必须接收 request_id 和 run_id。
- [ ] 同一 `session_id + request_id` 重复调用时返回已有 RunHandle 或已有终态。
- [ ] 同一 run_id 不得创建第二个 Run。
- [ ] ReplyStart/ReplyEnd 和 Assistant 持久化 metadata 必须包含 request_id/run_id。

### FR5：Inbox 使用 canonical 用户数据根

- [ ] 生产 Composition 显式注入 Inbox root。
- [ ] 不再以 `Path.cwd()/.ftre-inbox` 作为生产默认路径。
- [ ] 开发、安装、免安装和升级包使用同一用户数据根。
- [ ] 缺少 root 时返回结构化配置错误，不能静默创建安装目录队列。

### FR6：Steer 不跨 Run

- [ ] Steer 记录 target_run_id。
- [ ] `agent/before-reasoning` 只消费 target Run 仍 active 的 Steer。
- [ ] target Run 已取消、暂停或完成时，不得注入后续新 Run。
- [ ] 本阶段不改变 Steer 的“active Run 下一次 Reasoning 注入”产品语义。

## 4. 数据模型与状态约束

本阶段优先复用现有 `QueueItem`、lease、AgentView 和 Session metadata，仅增加缺失字段或状态原因，不创建第二套状态存储。

### 4.1 QueueItem 必需身份

```python
QueueItem(
    session_id: str,
    request_id: str,
    content: str | list[dict],
    placement: Literal["next_turn", "next_step"],
    target_run_id: str | None,
    sequence: int,
)
```

### 4.2 Agent 终态结果

```python
AgentRunResult(
    request_id: str,
    run_id: str,
    status: Literal[
        "completed", "failed", "cancelled", "interrupted", "paused"
    ],
    terminal_reason: str | None,
    retryable: bool = False,
)
```

`retryable` 只表示上层是否可以显式重试，不表示 Inbox 可以自动把原请求放回 pending。

### 4.3 状态规则

```text
pending -> inflight -> completed
pending -> inflight -> cancelled/failed/interrupted/paused
inflight -> pending       仅 Agent 未开始且准备阶段失败
completed/failed/cancelled/interrupted/paused -> pending  禁止
```

## 5. 接口和事件调整

### 5.1 Agent 状态

保留现有 `AgentView.state` 供客户端展示；新增或补齐终态原因：

```json
{
  "state": "idle",
  "idle_reason": "completed",
  "run_id": "run-001",
  "request_id": "req-001"
}
```

更推荐直接消费带身份的生命周期事件：

```text
agent/run-started
agent/run-completed
agent/run-failed
agent/run-cancelled
agent/run-interrupted
agent/run-paused
```

### 5.2 Session UserMessage

`emit_user_message_if_absent()` 语义必须从“按稳定 ID upsert”收紧为“存在即返回”：

```python
message = await session_events.emit_user_message_if_absent(
    session_id,
    channel_id,
    request_id=request_id,
    content=content,
    run_id=run_id,
)
# 已存在：返回原消息，不修改 created_at/content
```

### 5.3 Queue Operation Response

沿用 F24 的 `revision` 和完整快照；可增加冻结原因，但不得暴露内部 lease：

```json
{
  "type": "session/queue",
  "request_id": "op-001",
  "ok": true,
  "payload": {
    "session_id": "ws_sess_1",
    "revision": 12,
    "items": [],
    "frozen": true,
    "frozen_reason": "cancelled"
  }
}
```

客户端不能仅凭 `items=[]` 或 `state=idle` 判断“可以继续发送”。

## 6. 三阶段实施计划

每个阶段都执行：读基线 → 修改 → 专项测试 → 全量门禁 → commit → 更新 TODO/变更记录。阶段之间不夹带额外重构。

### F38-A：错误回队与错误消费修复

**目标**：先消除当前最严重的重复执行和队列阻塞。

**修改范围**：

- `packages/ftre-inbox/src/ftre_inbox/service.py`
- `packages/ftre-inbox/src/ftre_inbox/repository.py`
- `packages/ftre-agent/src/ftre_agent/service.py`
- Agent 状态/结果契约及对应测试

**实现要求**：

1. 区分“Agent 未开始”和“Agent 已开始”两个时间点。
2. 已开始执行的 cancelled/failed/interrupted 不再 release。
3. 删除基于普通 idle 的 worker 唤醒；仅 `completed` 触发 next-turn。
4. 取消、暂停、失败、中断后设置队列冻结原因。
5. 同一请求重复进入 worker 时先查询已有 request/run 事实，禁止第二次 Agent.run。

**验收**：

```text
A running -> cancel -> A 不回 pending
A cancel -> enqueue B -> B 不被 A 抢占
A completed -> B 只执行一次
A paused/failed/interrupted -> B 保持 pending
```

**状态**：已实现并通过专项测试与全量后端测试。

**Commit**：`fix(F38): 修复 Inbox 终态回队与错误消费`

### F38-B：消息身份、Agent 幂等与存储根修复

**目标**：消除刷新后消息乱序和不同安装包队列分叉。

**修改范围**：

- `src/ftre/services/session/events.py`
- `src/ftre/services/session/projection.py`
- `src/ftre/services/session/persistence/repository.py`
- `packages/ftre-agent-runtime/src/ftre_agent_runtime/react_runner.py`
- `packages/ftre-inbox/src/ftre_inbox/plugin.py`
- Gateway Composition 与配置测试

**实现要求**：

1. 已有 UserMessage 不覆盖 created_at/content/metadata。
2. ReplyStart/Assistant metadata 始终保存 request_id/run_id。
3. `AgentService.run()` 相同 request_id 返回原 Run/终态。
4. Inbox root 由 Host 显式注入 canonical 用户目录；删除生产 cwd fallback。
5. 工作区源码、wheel、免安装包和安装包使用同一版本实现。

**验收**：

- 重复投递只有一个 UserMessage，原时间和历史顺序不变。
- 刷新前后用户消息与 Assistant 顺序一致。
- 同 request_id 重连、重试、恢复不创建第二个 Run。
- 启动日志显示 Inbox canonical root，安装目录不出现新 `.ftre-inbox`。

**Commit**：`fix(F38): 收口请求幂等与 Inbox 存储根`

### F38-C：故障注入、Steer 边界与跨仓验收

**目标**：验证修复在实际生命周期和发行包中生效。

**修改范围**：

- `packages/ftre-inbox` Steer target_run_id 与 Hook 测试
- `tests/contracts/`
- `tests/lifecycle/`
- `tests/integration/`
- `tests/architecture/`
- 配对 `E:\binn\ftre-desktop` 的 queue/status/reconnect 测试

**故障点**：

```text
claim 前
claim 后、UserMessage 前
UserMessage 后、Agent.run 前
Agent.run 已接受后
Assistant 已输出后
terminal 持久化前后
Gateway 重连/客户端刷新
```

**验收**：

1. 每个故障点都不产生重复 UserMessage。
2. Agent 已接受的请求不回 pending。
3. 普通队列只由 RunCompleted 推进。
4. Steer 只注入 target_run_id 对应的 active Run。
5. 发行包实际加载的代码包含 request_id/run_id metadata 修复。

**Commit**：`test(F38): 完成 Inbox 恢复与跨仓验收`

## 7. 测试计划

### 7.1 后端单元测试

- `repository.release()` 在未开始/已开始两个阶段的差异。
- Agent 终态原因到 Inbox worker 行为的映射。
- completed 唤醒与其他终态冻结。
- request_id/run_id 重复调用幂等。
- UserMessage created_at/content/metadata 不可变。
- canonical root 缺失、显式注入和升级路径。

### 7.2 恢复与并发测试

- A 执行后取消，再提交 B，A 不重跑且 B 不被旧 A 抢占。
- cancel 与 Worker finalize 并发。
- duplicate prompt 与 WebSocket reconnect 并发。
- Gateway 启动扫描不自动发送 pending。
- 两个 Worker 对同一 request_id 只能一个成功执行。

### 7.3 客户端回归

- queue revision 单调。
- USER_MESSAGE 与 queue snapshot 乱序时最终一致。
- 刷新后不自动触发旧请求。
- 空队列不被解释为 completed。
- frozen_reason 正确展示或保持不可见但不误发送。

## 8. 观测和诊断

所有状态迁移日志至少包含：

```text
session_id request_id run_id state_from state_to terminal_reason lease_action queue_revision
```

重点指标：

- `inbox_requeue_total{reason}`：Agent 已开始后的 cancel 不应增加。
- `request_duplicate_run_total`：必须为 0。
- `request_transition_total{from,to}`。
- `inbox_frozen_total{reason}`。
- `user_message_conflict_total`。
- `request_recovery_total{action}`。

日志不得记录 API Key、完整用户附件、完整 System Prompt 或完整 Tool 参数。

## 9. 验收命令

每阶段至少运行：

```powershell
python -m pytest -q
python -m ruff check src tests packages
git diff --check
```

架构扫描必须证明：

```text
生产路径不使用 Path.cwd()/.ftre-inbox
Agent 已开始后不存在 repository.release()
普通 queue 消费不由无原因 idle 触发
UserMessage 已存在时不会生成新的 created_at
AgentService.run 必须带 request_id/run_id
```

## 10. 完成定义

- [ ] F38-A 重复回队和错误消费已修复。
- [ ] F38-B UserMessage、Assistant request/run metadata 和 Inbox root 已收口。
- [ ] F38-C 故障注入、Steer、重连、发行包和跨仓测试通过。
- [ ] 取消、暂停、失败、中断不会自动消费后续普通队列。
- [ ] 正常完成后队列最多推进一条，且不会重复执行。
- [ ] 未引入第二套 Request/Inbox/Agent 状态机；长期 Ledger/SQLite 另立阶段评估。
- [ ] 全量 pytest、Ruff、架构扫描、Package 构建和 Gateway/Desktop smoke 全部通过。

## 11. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-29 | 初始版本 | 记录 Inbox 重复投递、idle 误消费、消息时间覆盖、运行幂等和安装目录持久化问题 |
| 2026-08-29 | 收缩为 F38-A/F38-B/F38-C；移除本阶段独立 RequestLedger、SQLite、TurnCoordinator 目标 | 当前 Bug 可由现有 Owner 和状态模型修复，避免过度设计；长期账本作为后续阶段候选 |
| 2026-08-29 | 完成 F38-A：执行后请求不再 release 回队；取消/失败/中断冻结队列；普通 worker 仅在正常完成后推进；新增取消后新消息不被旧请求抢占的回归测试 | F38-A 专项 26 passed，全量 pytest 738 passed；目标行为已验证 |
