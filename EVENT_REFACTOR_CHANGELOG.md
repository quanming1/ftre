# Event 协议改造 — 2026-07-10

## 背景

将旧的 `done` + `error` 两个独立事件合并为统一的 `StepEvent`（`type="step"`），引入 Turn 概念（`turn_id`），在 `AgentEvent` 顶层增加 `timestamp`，并将 `user_message` 的产出权从 Gateway 下沉到 core。

## 新的事件结构

```json
{
  "type": "step",
  "event_id": "a1b2c3d4e5f6a7b8",
  "timestamp": 1783671854.14,
  "turn_id": "turn_abc123def456",
  "data": { "phase": "turn_end", "success": true, "reason": "completed", "iterations": 3 }
}
```

### 顶层字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | EventType 枚举值 |
| `event_id` | string | 16 位 hex，自动生成 |
| `timestamp` | float | Unix 秒，事件创建时刻（不是入库时刻），与 DB `messages.timestamp` 列对齐 |
| `turn_id` | string | `turn_` + 12 位 hex，由 `RunState.start()` 生成，`run()` 中统一盖戳到所有事件；空串不出现在序列化中 |
| `data` | object | 事件负载 |

### EventType 枚举（6 个值，旧值已删除）

| 值 | 说明 |
|----|------|
| `tool_result` | 工具执行结果 |
| `assistant_message` | 流式 assistant 消息快照 |
| `assistant_message_complete` | LLM 一轮完整消息 |
| `step` | Turn 生命周期事件 |
| `retry` | LLM 重试 |
| `user_message` | 用户输入 / 工具注入的 user message |

已删除：`done`、`error`

### StepEvent

```
phase=turn_start:  Turn 开始（入库 + WS）
phase=turn_end:    Turn 结束（入库 + WS）
  reason="completed"       → 正常完成
  reason="max_iterations"  → 达到迭代上限
  reason="cancelled"       → 用户取消
  reason="error"           → LLM 错误（携带 error_message / error_code）
```

data 字段：`phase`、`success`、`reason`、`iterations`、`error_message`（reason=error 时）、`error_code`（reason=error 时）

**所有 StepEvent（含 turn_start）都持久化到 DB。**

## 事件时序

```
step(turn_start)           ← Turn 开始（core yield，Gateway 入库 + WS）
user_message               ← 用户输入（core yield，Gateway 入库 + echo）
...assistant events...      ← assistant_message / assistant_message_complete / tool_result
step(turn_end)             ← Turn 结束（core yield，Gateway 入库 + WS）
```

core `run()` 中的事件产出顺序：
1. `state.start()` → 生成 turn_id
2. yield `StepEvent(TURN_START)` → 盖 turn_id
3. 从 `runtime_context["user_input"]` 取用户输入 → yield `UserMessageEvent` → 盖 turn_id
4. 写 memory
5. `_loop()` → yield 各种事件 → 每个都盖 turn_id
6. yield `StepEvent(TURN_END)` → 盖 turn_id

## user_message 的新流程

### 改造前

```
loop.py Step 6:   save_message(user_message)     ← 无 turn_id，timestamp = time.time()
loop.py Step 6.5: echo user_message              ← 无 turn_id，无 timestamp
loop.py Step 8:   agent.run()                    ← core 生成 turn_id
```

问题：user_message 没有 turn_id，timestamp 比 step 事件早。

### 改造后

```
loop.py:   runtime_context["user_input"] = {content, metadata}
loop.py:   agent.run(messages, runtime_context)
  core:    state.start() → 生成 turn_id
  core:    yield step(turn_start)               ← 有 turn_id + timestamp
  core:    yield user_message(content)           ← 有 turn_id + timestamp
  core:    yield ...assistant events...          ← 有 turn_id + timestamp
  core:    yield step(turn_end)                  ← 有 turn_id + timestamp
loop.py:   async for event:
             UserMessageEvent(hide=False) → save_message(timestamp=event.timestamp) + echo(inbound.data + event.turn_id/timestamp)
             StepEvent / _PERSISTENT_CLASSES → save_message(timestamp=event.timestamp) + publish_outbound(event.to_dict())
```

Gateway 在 async for 中拦截 `UserMessageEvent`（`hide=False`）：
- 持久化：用 `event._data_dict()` + `event.event_id` + `attachments` + `event.timestamp`
- echo：用 `inbound.data`（原始 UI 格式）+ `inbound.metadata`（含 frame_id）+ core 的 `event.timestamp` / `event.turn_id`
- `continue` 跳过通用持久化/发布路径

hidden user_message（工具注入的）走通用 `_PERSISTENT_CLASSES` 路径，不 echo。

## 各文件改动

### ftre-agent-core

#### `src/ftre_agent_core/agent/event.py`
- 删 `EventType.DONE` / `EventType.ERROR`
- 删 `DoneEvent` / `ErrorEvent` 类
- 删 `_from_type` 中的 `done` / `error` 分支
- 新增 `EventType.STEP` / `StepPhase` / `StepEvent` / `step_event()`
- `AgentEvent` 基类新增 `timestamp: float`（`init=False`, `default_factory=time.time`）和 `turn_id: str`（`init=False`, 默认 `""`）
- `to_dict()` 顶层输出 `timestamp`，`turn_id` 非空时输出
- `from_dict()` 反序列化时恢复 `timestamp` 和 `turn_id`
- 删 `AgentEventDict`、`DoneData`、`ErrorData`、`StepData` TypedDict（无人消费）
- 删 `done_event()` / `error_event()` 工厂函数

#### `src/ftre_agent_core/agent/__init__.py`
- 删 `DoneEvent` / `ErrorEvent` / `AgentEventDict` / `DoneData` / `ErrorData` 导出
- 新增 `StepPhase` / `StepEvent` 导出

#### `src/ftre_agent_core/agent/runner/react_runner.py`
- `RunState` 新增 `turn_id` 字段，`start()` 中生成 `turn_{12-hex}`
- `run()` 中 turn_start 后 yield user_message（从 `runtime_context["user_input"]` 取）
- `run()` 主循环给每个 event 盖 `turn_id` 戳
- `_loop()` / `_run_turn()` / `_stream_turn()` 中所有 `done_event()` / `error_event()` → `step_event(TURN_END, ...)`
- error + done 对合并为单条 `step_event(TURN_END, reason="error", error_message=..., error_code=...)`

#### `src/tests/test_simplify_verification.py`
- `test_existing_types`: 去掉 `done`/`error`，加入 `step`
- `test_removed_types_not_exist`: 加入 `done`/`error`
- `test_removed_event_classes`: 加入 `DoneEvent`/`ErrorEvent`
- 删旧 `test_done_event` / `test_error_event` 等测试
- 新增 `test_step_event`
- `test_agent_event_to_dict`: 加 `assert "timestamp" in d`
- 删 `AgentEventDict` 相关断言

#### `src/tests/test_cancel_live.py` / `test_parallel_cancel.py`
- `EventType.DONE` → `EventType.STEP` + `phase == "turn_end"` 过滤

### ftre (Gateway)

#### `src/ftre/agent/loop.py`
- import 改为 `StepEvent, StepPhase`（删 `DoneEvent, ErrorEvent`）
- `_PERSISTENT_CLASSES` 移除 `DoneEvent, ErrorEvent`
- 删旧 Step 6（save_message）+ Step 6.5（echo）
- 新增 `runtime_context["user_input"] = {content, metadata}`
- async for 新增 `UserMessageEvent`（`hide=False`）拦截：持久化 + echo + `continue`
- 持久化逻辑改为 `(self._PERSISTENT_CLASSES, StepEvent)` 统一处理，`save_message(timestamp=event.timestamp)`
- except 块中 `DoneEvent(...)` → `StepEvent(TURN_END, ...)`

#### `src/ftre/channel/ws_channel.py`
- `VOLATILE_CLEAR_ALL_TYPES` = `frozenset({"step", "retry"})`（删 `done`/`error`）

#### `src/ftre/session/manager.py`
- 删 `migrate_events_to_messages` 调用

#### `src/ftre/session/migrate_events.py`
- **整个文件删除**

### ftre-desktop (前端)

#### `packages/renderer/src/services/websocket-client.ts`
- `AgentEvent` 接口新增 `timestamp?: number` 和 `turn_id?: string`

#### `packages/renderer/src/stores/chat.ts`
- WS live 构造 `BusEvent` 时从 `ev.timestamp` 取时间戳：`ts: typeof ev.timestamp === "number" ? ev.timestamp * 1000 : undefined`
- 新增 `case "step"`：`turn_start` 设 isBusy=true，`turn_end` seal streaming + isBusy=false + error 处理
- 删 `case "done"` / `case "error"`
- token refresh 触发条件删 `done`
- `TokenUsage.anchor.source` 去掉 `"done"`

#### `packages/renderer/src/stores/session.ts`
- 历史回放优先从 `r.data.timestamp` 取时间戳，兜底 `r.timestamp`

#### `packages/renderer/src/services/api.ts`
- `SessionMessage.type` 注释更新
- `TokenUsage.anchor.source` 去掉 `"done"`

### ftre-docs

#### `src/content/agent-events.md`
- 顶层字段说明新增 `timestamp` / `turn_id`
- 事件类层次图删 `DoneEvent` / `ErrorEvent`，新增 `StepEvent`
- 事件类型总览表删旧行，`step` 说明更新
- `step` 事件详细定义（含 `turn_start` / `turn_end`）
- 删旧 `error` / `done` 详细定义
- 时序图加 `user_message`，`turn_start` 不再标"ephemeral"
- 退出路径表 `done.*` → `step.*`
- changelog 更新

## DB

- 旧 `sessions.db` 已备份为 `sessions.db.bak.20260710`
- 原文件因被运行中的后端锁定，需手动停止后端后删除
- 下次启动时 `SessionManager.init()` 会自动建新库（`CREATE TABLE IF NOT EXISTS`）
- 新库无旧格式数据，不需要 `migrate_events.py`

## 前端 applyEvent 的 step case

```typescript
case "step": {
  const phase = d.phase;
  if (phase === "turn_start") {
    b.isBusy = true;
    b.sessionStatus = "running";
    b.error = null;
    b.retryState = null;
    return;
  }
  // phase === "turn_end"
  replaceTail((m) => m.streaming ? { ...m, streaming: false } : m);
  b.isBusy = false;
  b.sessionStatus = "idle";
  b.retryState = null;

  if (d.reason === "error" && d.error_message) {
    const msg: string = d.error_message;
    const code = d.error_code;
    b.messages = [
      ...b.messages,
      { id: ev.frameId ?? nextId("err"), role: "assistant", content: msg, timestamp: ts, isError: true },
    ];
    b.error = code ? `[${code}] ${msg}` : msg;
  }
  return;
}
```

## 历史回放时序

历史回放时 `turn_start` 会从 DB 加载，`applyEvent` 会设 `isBusy=true`。但同一 turn 的 `turn_end` 随后到来会设回 `isBusy=false`。最后一个 turn 如果没有 `turn_end`（正在运行中），`isBusy=true` 是正确的。

## 注意事项

1. **重启前必须删除 `~/.ftre/sessions.db`**（当前被进程锁定，需先停后端）
2. 备份文件 `sessions.db.bak.20260710` 可用于恢复旧数据
3. `manager.py` 中的 `_migrate_messages_extract_fields` 是 schema 级迁移（非事件格式）：将 data JSON 中的 `event_id` / `turn_id` 提取到独立列，并从 data 中移除。新库不会触发，保留无害
4. `loop.py` 的 except 块中 `CancelledError` / `Exception` 路径会补发 `StepEvent(TURN_END)`，fallback 事件的 `turn_id` 从 `agent.state.turn_id` 恢复（`state.start()` 在 `run()` 首行已执行），同时持久化到 DB 确保历史回放时 turn 有完整的 `turn_end`

## DB Schema 对齐（2026-07-10 追加）

`messages` 表的 `data` 列原为万能 JSON 桶，`event_id` / `turn_id` 等公共字段混在事件特有字段中。现已对齐 AgentEvent 模型：

```sql
CREATE TABLE messages (
    id          TEXT PRIMARY KEY,   -- = event_id
    session_id  TEXT NOT NULL,
    type        TEXT NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}',  -- 只存事件特有字段
    timestamp   REAL NOT NULL,
    turn_id     TEXT NOT NULL DEFAULT ''     -- 新增独立列
);
```

`save_message` 签名变更：

```python
# 改前
async def save_message(self, session_id, type, data, *, timestamp=None) -> str

# 改后
async def save_message(self, session_id, type, data, *, event_id=None, turn_id="", timestamp=None) -> str
```

`data` 不再包含 `event_id` / `turn_id`，调用方通过关键字参数传入。前端 `SessionMessage` 接口新增 `turn_id` 字段，`session.ts` 历史回放从行级 `r.id` 取 eventId（不再从 `r.data.event_id` 挖）。
