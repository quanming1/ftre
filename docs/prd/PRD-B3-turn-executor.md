# PRD-B3-TurnExecutor状态机

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | B3 |
| 名称 | TurnExecutor 状态机（Turn 生命周期 + Hook 集成 + 命令处理） |
| 状态 | 已验收 |
| 创建日期 | 2026-08-12 |
| 定稿日期 | 2026-08-12 |
| 验收日期 | 2026-08-12 |
| 关联文档 | docs/TODO.yaml 阶段 B3；AGENTS.md |

## 1. 背景与目标

- **背景**：Turn 生命周期需要状态机驱动——从领取（advance）到构建上下文（build）到压缩（compact）到收尾（finalize），每一步有明确的前置和后置条件。同时需要集成 Hook 系统，在正确时机触发 before_messages_build 和 before_agent_run。
- **目标**：实现 TurnExecutor 状态机——`_advance → _build → _compact → _finalize` 四阶段流转，Hook 集成，命令处理（`/cancel`、`/compact`），SessionProjection 事件发布，TurnOutcome 返回。
- **非目标**：不实现压缩算法本身（B2）、不实现 mailbox 队列管理（B1）。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：Turn 状态机——`_advance`（领取 pending）→ `_build`（构建上下文）→ `_compact`（压缩检查）→ `_finalize`（收尾发布）四阶段流转
- [x] FR2：Hook 集成——`before_messages_build` 在 events 加载后、to_openai_messages 前触发；`before_agent_run` 在 agent 创建后、agent.run() 前触发
- [x] FR3：命令处理——`/cancel` 取消当前 turn，`/compact` 触发手动压缩，命令绕过 agent 直接执行
- [x] FR4：SessionProjection 事件发布——turn 执行过程中通过 SessionProjection 发布状态变更（phase 更新）
- [x] FR5：TurnOutcome 返回——turn 完成后返回 `TurnOutcome`，含执行结果、耗时、是否压缩等信息

### 2.2 非功能需求

- 性能：Turn 状态流转无阻塞等待（除 LLM 调用）
- 安全：命令处理不执行 agent，防止 `/cancel` 等 DoS
- 兼容性：TurnOutcome 结构稳定，新增字段不破坏旧消费者

## 3. 技术方案

### 模块设计

| 文件 | 职责 |
|---|---|
| `src/ftre/agent/turn_executor.py` | `TurnExecutor`，状态机 + Hook 集成 + 命令处理 + 事件发布 |

### 关键数据结构

```python
@dataclass
class TurnOutcome:
    success: bool
    duration_ms: int
    compacted: bool
    error: str | None

class TurnExecutor:
    async def _advance(self) -> QueueItem | None: ...    # 领取 pending
    async def _build(self, item: QueueItem) -> None: ...  # 构建上下文
    async def _compact(self) -> None: ...                 # 压缩检查
    async def _finalize(self, outcome: TurnOutcome) -> None: ...  # 收尾
```

### Hook 调用点

```
_advance → _build → [before_messages_build] → [before_agent_run] → agent.run() → _compact → _finalize
```

## 5. 验收标准

- [x] AC1：Turn 状态正确流转——从 `_advance` 到 `_finalize` 依次执行，无跳过或回退
- [x] AC2：Hook 在正确时机触发——`before_messages_build` 在 messages 构建前，`before_agent_run` 在 agent.run() 前
- [x] AC3：命令绕过 agent 执行——`/cancel` 和 `/compact` 不经过 agent LLM 调用，直接处理并返回
