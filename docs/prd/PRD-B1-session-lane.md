# PRD-B1-SessionLane架构

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | B1 |
| 名称 | AgentLoop SessionLane 架构（SessionLaneRegistry + MailboxStore + ContextGate + CompletionRegistry + TurnExecutor） |
| 状态 | 已验收 |
| 创建日期 | 2026-08-12 |
| 定稿日期 | 2026-08-12 |
| 验收日期 | 2026-08-12 |
| 关联文档 | docs/TODO.yaml 阶段 B1；AGENTS.md |

## 1. 背景与目标

- **背景**：旧 AgentLoop 用 `asyncio.Lock` 串行化所有 session，无法保证 durable admission（崩溃后 pending 丢失）和精确等待（无法按 request_id 等待特定 turn 完成）。随着并发 session 数量增长，需要按 session 隔离的 actor 模型。
- **目标**：实现 SessionLane 架构——每个 session 有唯一 worker（SessionLane），MailboxStore 持久化 pending 队列，ContextGate 水位检查，CompletionRegistry 精确等待，at-most-once 领取语义。
- **非目标**：不实现压缩逻辑（B2）、不实现 Turn 状态机细节（B3）。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：SessionLaneRegistry 每 session 唯一 worker——维护 lane 生命周期，保证一个 session 只有一个 lane，支持懒创建
- [x] FR2：MailboxStore request_id 幂等持久队列——pending 消息持久化到存储，request_id 去重防止重复入队
- [x] FR3：ContextGate 水位检查——领取下一条请求前检查上下文水位，必要时等待压缩完成
- [x] FR4：CompletionRegistry 精确等待——按 request_id 注册完成回调，等待特定 turn 完成后通知
- [x] FR5：at-most-once 领取语义——pending 被取走后 Gateway 崩溃时不自动重放，已写入 messages 的 UserMessage 保留

### 2.2 非功能需求

- 性能：不同 session 可并行执行，无全局锁
- 安全：同一 session 任意时刻最多一个 active turn
- 兼容性：不变量——turn 与 compaction 不会并发

## 3. 技术方案

### 模块设计

| 文件 | 职责 |
|---|---|
| `src/ftre/agent/session_lane.py` | `SessionLane`，单 session actor，FIFO + 取消 + 压缩门控 + 状态发布 |
| `src/ftre/agent/mailbox_store.py` | `MailboxStore`，request_id 幂等持久队列 |
| `src/ftre/agent/context_gate.py` | `ContextGate`，水位检查 + 压缩等待 |
| `src/ftre/agent/completion_registry.py` | `CompletionRegistry`，精确等待（按 request_id） |
| `src/ftre/agent/loop.py` | `AgentLoop`，按 session_id 路由到对应 lane |
| `src/ftre/agent/turn_executor.py` | `TurnExecutor`，执行单个已领取的 turn |

### 关键数据结构

```python
@dataclass
class QueueItem:
    request_id: str
    content: str
    phase: str            # "pending" | "active" | "done"

class SessionLane:
    session_id: str
    mailbox: MailboxStore
    gate: ContextGate
    completion: CompletionRegistry
```

### 不变量

- 不同 session 可以并行执行
- 同一个 session 任意时刻最多一个 active turn
- turn 与 compaction 不会并发

## 5. 验收标准

- [x] AC1：同 session 最多一个 active turn——并发提交多条消息到同一 session，只有一条在执行
- [x] AC2：turn 与 compaction 不并发——压缩进行中时新 turn 等待，不并发执行
- [x] AC3：pending 持久化可恢复——提交 pending 后重启 Gateway，pending 队列恢复
- [x] AC4：request_id 幂等——相同 request_id 重复提交不会产生重复 pending
