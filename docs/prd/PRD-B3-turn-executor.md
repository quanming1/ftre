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

- **背景**：一条消息被 SessionLane 领取后，需要由独立执行器完成命令判断、上下文构建、Hook 注入、Agent/工具运行和收尾，并返回结构化结果。领取前和回合后的压缩门控属于 B1/B2，不应混入单轮执行器。
- **目标**：实现 TurnExecutor 的单轮执行——`COMMAND → BUILD → RUN → FINALIZING`，集成 Hook、命令处理、SessionProjection 事件发布和 `TurnOutcome` 返回。
- **非目标**：不实现压缩算法本身（B2）、不管理 mailbox 领取/FIFO/worker/压缩门控（B1）。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：单轮状态机——对已经由 SessionLane 领取的请求执行 `COMMAND`（命令匹配和 UserMsg 持久化）→ `BUILD`（上下文与 Hook）→ `RUN`（Agent/工具/LLM）→ `FINALIZING`（收尾）
- [x] FR2：Hook 集成——`before_messages_build` 在 events 加载后、to_openai_messages 前触发；`before_agent_run` 在 agent 创建后、agent.run() 前触发
- [x] FR3：命令处理——普通命令在当前已领取 Turn 内执行；`/cancel` 使用独立的 `turn_cancel` 控制消息取消 active turn，不伪装成用户消息；`/compact` 由命令处理层触发手动压缩
- [x] FR4：SessionProjection 事件发布——turn 执行过程中通过 SessionProjection 发布状态变更（phase 更新）
- [x] FR5：TurnOutcome 返回——turn 完成后返回 `turn_id`、`status`、`user_message_id`、`final_content` 和结构化 `error`
- [x] FR6：取消/异常收尾——无论正常、取消还是异常，都关闭开放 Reply、持久化可恢复快照并发送成对的 `PIPELINE_START/PIPELINE_END`
- [x] FR7：人工确认恢复——权限确认相关事件先 checkpoint 到 messages，恢复 Turn 时从持久历史重建 Agent 上下文，不重复写 UserMsg

### 2.2 非功能需求

- 性能：Turn 状态流转无阻塞等待（除 LLM 调用）
- 安全：命令处理不执行 agent，防止 `/cancel` 等 DoS
- 兼容性：TurnOutcome 结构稳定，新增字段不破坏旧消费者

## 3. 技术方案

### 模块设计

| 文件 | 职责 |
|---|---|
| `src/ftre/agent/turn_executor.py` | `TurnExecutor`，单轮状态机 + Hook 集成 + 命令处理 + 事件发布；不拥有队列和压缩 |

### 关键数据结构

```python
@dataclass(frozen=True)
class TurnOutcome:
    turn_id: str
    status: str                 # completed | error | cancelled
    user_message_id: str = ""
    final_content: str = ""
    error: dict | None = None

class TurnStatus(str, Enum):
    COMMAND = "command"
    BUILDING = "building"
    RUNNING = "running"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"

class TurnExecutor:
    async def execute(
        self, inbound: BusMessage, *, turn_id: str | None = None,
        config: AgentConfig | None = None,
        agent_profile: AgentProfile | None = None,
    ) -> TurnOutcome: ...
    async def _advance(self, turn: Turn) -> TurnStatus: ...  # 只做状态分派，不领取队列
    async def _command(self, turn: Turn) -> TurnStatus: ... # 命令匹配并持久化 UserMsg
    async def _build(self, turn: Turn) -> TurnStatus: ...   # 上下文与 Hook
    async def _run(self, turn: Turn) -> TurnStatus: ...     # Agent/工具/LLM
    async def _finalize(self, turn: Turn) -> TurnStatus: ... # 收尾
```

### Hook 调用点

```
SessionLane.take → execute → _advance → _command → _build → [before_messages_build] → [before_agent_run] → _run → _finalize → TurnOutcome
```

## 4. 接口与运行契约

- `SessionLane` 已经完成 `MailboxStore.take`，所以 TurnExecutor 不再等待锁、不再领取队列；其中 `_advance` 只是根据当前 `TurnStatus` 分派 `_command/_build/_run`。
- `ContextGate` 在 `execute` 之前和之后负责压缩水位；TurnExecutor 不启动自动压缩，也不决定下一条何时执行。
- `_command` 阶段才把已领取的输入通过 `SessionProjection` 写入 `messages`；仍在 pending 的消息不会进入当前上下文。
- `AgentLoop.emit_session_event` 先更新 Projection，再通过 EventBus 广播，保证客户端看到的事件与持久化顺序一致。

### 4.1 持久化与事件顺序

1. `_command` 先将原始 inbound 写成 UserMsg（metadata.request_id），再执行普通命令或进入 BUILDING。
2. `SessionProjection` 接收 Reply/CustomEvent：ReplyStart、完整 block、tool call/result、模型调用结束和 HITL 确认边界立即 checkpoint；高频 delta 允许节流写盘。
3. `AgentLoop.emit_session_event` 遵循“Projection 落盘成功 → EventBus outbound 广播”；客户端实时事件不是历史事实源。
4. `REPLY_END` 或取消/异常收尾后，SessionProjection 将 assistant Msg 写成终态；TurnExecutor 返回 `TurnOutcome`，SessionLane 再完成 request waiter 并推进 mailbox revision。

### 4.2 命令与取消分类

| 输入 | 是否进入 mailbox | 是否写 UserMsg | 是否调用 Agent |
|---|---:|---:|---:|
| 普通文本 | 是 | 领取后是 | 是 |
| 普通命令（如 `/compact`） | 是 | 由命令定义决定 | 由 handler 决定 |
| `turn_cancel` 控制消息 | 否 | 否 | 取消当前 active |
| `send_message notify` | 否（旁路） | 目标写 external AssistantMsg | 否 |

## 5. 验收标准

- [x] AC1：Turn 状态正确流转——已领取的 Turn 经 `_advance` 分派，依次经过 `COMMAND → BUILDING → RUNNING → FINALIZING`（命令短路场景可提前完成），无跳过或回退
- [x] AC2：Hook 在正确时机触发——`before_messages_build` 在 messages 构建前，`before_agent_run` 在 agent.run() 前
- [x] AC3：命令绕过 agent 执行——`/cancel` 和 `/compact` 不经过 agent LLM 调用，直接处理并返回
- [x] AC4：取消/异常总有终态——开放 Reply 被标记 interrupted/error，`PIPELINE_END` 必达，SessionLane 收到 cancelled/error 的 TurnOutcome
- [x] AC5：Projection 顺序——attach/历史读取到的 Msg 不会早于对应的落盘事件；高频 delta 可节流但语义边界必须 checkpoint
- [x] AC6：HITL 恢复——Gateway 重启或确认后可从持久 Msg 恢复 tool_call 状态，不重复创建 UserMsg

## 6. 测试计划

- `tests/test_turn_lifecycle.py`：COMMAND/BUILDING/RUNNING/FINALIZING、命令短路、PIPELINE_START/END、取消和异常。
- `tests/test_turn_hitl.py`：权限确认、ASKING/ALLOWED/FINISHED checkpoint 和恢复。
- `tests/test_session_projection.py`：Reply Msg 聚合、节流 checkpoint、REPLY_END 终态、attach snapshot。
- `tests/test_session_lane.py`：TurnOutcome 与 CompletionRegistry、队列 drain 和 cancel 后继续消费。

## 7. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-08-13 | 将旧的 `_advance → _build → _compact → _finalize` 修正为 SessionLane 领取、TurnExecutor 单轮执行、ContextGate 外置压缩门控；同步更新 TurnOutcome 字段和取消语义 | 当前实现已完成职责拆分，旧描述会误导后续开发者把队列/压缩重新放回 TurnExecutor |
| 2026-08-13 | 补充 TurnStatus、Projection checkpoint 顺序、取消/异常收尾、HITL 恢复和命令分类；增加可执行测试计划 | 完善单轮执行与持久历史之间的边界，避免把实时 Event 当作持久事实 |
| 2026-08-13 | 影响复核：AC1-AC6 对应 Turn/Projection/HITL 定向测试通过；本次仅改文档 | 记录执行器协议细化的验收依据 |
