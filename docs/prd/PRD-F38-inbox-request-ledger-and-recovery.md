# PRD-F38 Inbox 重复投递与恢复幂等修复

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F38 |
| 名称 | Inbox 重复投递、终态消费与恢复幂等修复 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-29 |
| 定稿日期 | 2026-08-29 |
| 验收日期 | 2026-08-29 |
| 关联文档 | `docs/TODO.yaml` F38；`docs/prd/PRD-F35-agent-service-inbox-message-boundary.md`；`docs/prd/PRD-F23-steering-message-boundary.md`；`docs/prd/PRD-F24-queue-operation-response.md`；`AGENTS.md` |

> 本版本是对原 F38 草案的范围收缩。当前阶段只修复已复现的重复投递、错误消费、消息时间覆盖、运行幂等和存储路径问题；不提前引入独立 RequestLedger、SQLite 或新的调度器。Inbox 的最终实现进一步收敛为“接纳、排队、claim、交给 AgentService”，不保留 delivery lease 或回退状态机。

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

在不引入第二套调度器的前提下，建立以下不变量：

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
InboxRepository  -> Inbox 持久化、claim、snapshot
InboxService     -> admission、worker、AgentService 调用、队列 Hook
AgentService     -> Agent Run、状态和 request/run 幂等
SessionService   -> UserMessage/Assistant 持久化与 Projection
WebSocket        -> queue/status/event wire 协议
客户端           -> 服务端快照和事件投影
```

本阶段不重新划分 Package，只修正这些 Owner 之间的状态交接。

### 2.2 claim 的最小正确语义

```text
PENDING --claim（持久化移除）--> REMOVED
REMOVED --交给 AgentService--> completed/failed/cancelled/interrupted/paused
```

Inbox 不维护 REMOVED/Agent 运行状态，也不存在 `repository.release()`、lease timeout 或自动回退。claim 的持久化提交完成后，项目即不再属于队列；显式重试由上层生成新的 request。

### 2.3 不用 `idle` 作为消费触发器

Inbox 不把 `idle` 当作消费事件。worker 只在上一项 `AgentService.run()` 正常完成后继续取下一项；Agent 状态只作为交付前的接收门禁，不由 Inbox 持有或改变：

```text
RunCompleted  -> 消费下一条 next-turn
RunCancelled  -> worker 停止，pending 保留
RunFailed     -> worker 停止，pending 保留
RunInterrupted-> worker 停止，pending 保留
RunPaused     -> worker 停止，pending 保留
```

暂停、失败、取消和中断都会让 worker 停止；它们不会触发自动回退或重投。需要继续时由上层显式调用 `resume_pending()` 或提交新的 request。

## 3. 功能需求

### FR1：执行后请求不可重新入队

- [x] A 被 claim 后，无论 Agent 尚未开始、已写入 UserMessage、已执行 Tool 还是已产生 Assistant 输出，都不回 pending。
- [x] 终态请求保留 request_id/run_id 和 terminal reason，不能被普通 worker 再次 claim。
- [x] 旧 inflight 在进程关闭时不得无条件回排；恢复必须依赖 Session/Assistant 证据。

### FR2：普通队列只在正常完成后消费

- [x] 新会话首次显式发送仍可启动第一条消息。
- [x] active Run 期间新增普通消息只进入 pending。
- [x] `RunCompleted` 后最多消费一条队首 next-turn；消费完成后由下一个明确的完成事件继续推进。
- [x] cancel、pause、failed、interrupted、Gateway 恢复和普通 idle 不得自动发送 pending。
- [x] Agent 终态后新消息保留；显式恢复或新 request 不得被旧消息抢占。

### FR3：UserMessage 真正幂等

- [x] 通过 `session_id + request_id` 生成稳定 UserMessage/Event ID。
- [x] 已有消息命中时返回原 message_id、原 created_at、原 content、原 metadata，不重新覆盖。
- [x] 相同 request_id 但 content/attachments 不同返回 `request-content-conflict`。
- [x] 重放不改变 Session 数组顺序，不制造第二条用户消息。

### FR4：Agent Run 幂等

- [x] AgentService 的 `run()` 接收 request_id，并以该身份关联 run_id。
- [x] 同一 `session_id + request_id` 重复调用时返回已有终态。
- [x] 同一 run_id 不得创建第二个 Run。
- [x] ReplyStart/ReplyEnd 和 Assistant 持久化 metadata 包含 request_id/run_id。

### FR5：Inbox 使用 canonical 用户数据根

- [x] 生产 Composition 显式注入 Inbox root。
- [x] 不再以 `Path.cwd()/.ftre-inbox` 作为生产默认路径。
- [x] 开发、安装、免安装和升级包使用同一用户数据根。
- [x] 缺少 root 时返回明确配置错误，不能静默创建安装目录队列。

### FR6：Steer 不跨 Run

- [x] Steer 记录 target_run_id。
- [x] `agent/before-reasoning` 只消费 target Run 仍 active 的 Steer。
- [x] target Run 已取消、暂停或完成时，不得注入后续新 Run。
- [x] 本阶段不改变 Steer 的“active Run 下一次 Reasoning 注入”产品语义。

## 4. 数据模型与状态约束

本阶段复用现有 `QueueItem`、AgentView 和 Session metadata，仅保留队列本身需要的字段，不创建第二套状态存储。

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
pending --claim--> removed
removed --AgentService.run--> completed/failed/cancelled/interrupted/paused
completed/failed/cancelled/interrupted/paused 不回到 pending
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

沿用 F24 的 `revision` 和完整快照；队列快照只描述当前 pending 项，不暴露 Agent 终态或内部执行状态：

```json
{
  "type": "session/queue",
  "request_id": "op-001",
  "ok": true,
  "payload": {
    "session_id": "ws_sess_1",
    "revision": 12,
    "items": []
  }
}
```

客户端不能仅凭 `items=[]` 或 `state=idle` 推断某个历史请求会被重新执行。

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

1. claim 前只做 admission、Hook 和持久化校验。
2. claim 后直接调用 AgentService，不再 release 或 ack。
3. 删除基于普通 idle 的 worker 唤醒；仅正常完成后继续 next-turn。
4. 取消、暂停、失败、中断后 worker 停止且 pending 不被改写。
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

**状态**：已实现并通过专项测试与全量后端测试（742 passed）；目标模块 Ruff 与空白检查通过。

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
2. Agent 已接受或已 claim 的请求不回 pending。
3. 普通队列只由 RunCompleted 推进。
4. Steer 只注入 target_run_id 对应的 active Run。
5. 发行包实际加载的代码包含 request_id/run_id metadata 修复。

**Commit**：`test(tests): 完成 Inbox 恢复与跨仓验收`

**状态**：已实现；Steer 目标、恢复/重连边界、全量后端测试和三个 Package wheel 已验证。

## 7. 测试计划

### 7.1 后端单元测试

- claim 后项目永久离开 pending，Agent 失败时不回队。
- Agent 终态原因到 Inbox worker 行为的映射。
- completed 后继续消费与其他终态停止 worker、保留 pending。
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
- Agent 终态只通过 status/event 展示，不改变队列快照，也不误发送 pending。

## 8. 观测和诊断

所有 Inbox 领取/交付诊断至少包含：

```text
session_id request_id run_id stage status queue_revision
```

重点指标：

- `inbox_claim_total`：每个 request 只能成功 claim 一次。
- `request_duplicate_run_total`：必须为 0。
- `request_transition_total{from,to}`。
- `user_message_conflict_total`。
- `inbox_delivery_error_total{stage}`。

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
生产路径不存在 Inbox release/requeue 分支
普通 queue 消费不由无原因 idle 触发
UserMessage 已存在时不会生成新的 created_at
AgentService.run 必须带 request_id/run_id
```

## 10. 完成定义

- [x] F38-A 重复回队和错误消费已修复。
- [x] F38-B UserMessage、Assistant request/run metadata 和 Inbox root 已收口。
- [x] F38-C 故障注入、Steer、重连、发行包和后端跨仓边界测试通过。
- [x] 取消、暂停、失败、中断不会自动消费后续普通队列。
- [x] 正常完成后队列最多推进一条，且不会重复执行。
- [x] 未引入第二套 Request/Inbox/Agent 状态机；长期 Ledger/SQLite 另立阶段评估。
- [x] 全量 pytest、全仓 Ruff（显式 Python 文件清单）、架构扫描、Package 构建、Gateway smoke 和 Desktop renderer smoke 全部通过。

## 11. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-29 | 初始版本 | 记录 Inbox 重复投递、idle 误消费、消息时间覆盖、运行幂等和安装目录持久化问题 |
| 2026-08-29 | 收缩为 F38-A/F38-B/F38-C；移除本阶段独立 RequestLedger、SQLite、TurnCoordinator 目标 | 当前 Bug 可由现有 Owner 和状态模型修复，避免过度设计；长期账本作为后续阶段候选 |
| 2026-08-29 | 完成 F38-A：执行后请求不再 release 回队；取消/失败/中断停止 worker 但保留 pending；普通 worker 仅在正常完成后推进；新增取消后新消息不被旧请求抢占的回归测试 | F38-A 专项 26 passed，全量 pytest 738 passed；目标行为已验证 |
| 2026-08-29 | 完成 F38-B：Session UserMessage 存在即返回并拒绝 request 内容冲突；AgentService 缓存 request 终态；公开 SessionService.sessions_root 并禁止 Inbox 生产 cwd fallback | F38-B 专项 32 passed；全量 pytest 742 passed；目标模块 Ruff 与空白检查通过 |
| 2026-08-29 | 完成 F38-C：Steer 持久化 target_run_id，Hook 以 request_id 对齐 active Run，跨 Run 不注入；完成恢复/重连/故障边界专项和三个 Package wheel 验收 | F38-C 专项 58 passed；全量 pytest 744 passed；三个 wheel 构建成功并检查包含修复 |
| 2026-08-29 | 完成最终门禁：显式 Python 文件清单 Ruff 全部通过；Desktop renderer 55 files / 537 tests 通过；更新 F38 为已验收 | 发行包、Gateway 和客户端恢复边界均有可复核证据 |
| 2026-08-29 | 完成审计修复：未提供运行身份时不再消费已绑定 Steer；SessionProjection 增加 UserMessage 并发单写保护；最终全量 pytest 745 passed，三个 wheel 重新构建并清理 | 补齐跨 Run 与并发重放的真实边界 |
| 2026-08-29 | 按最终职责重构 Inbox：删除 `LeaseRecord`、`claim_lease/ack/release` 和执行回退分支；Repository 只持久化 pending 与原子 claim，Service 只负责 `Inbound → QueueItem → FIFO → AgentService`；旧 schema 的 inflight 仅清理、不重投 | 消除 Inbox 与 Agent Runtime 的职责重叠，降低状态机复杂度 |
