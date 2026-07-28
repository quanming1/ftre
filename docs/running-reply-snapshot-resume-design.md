# 进行中 Reply 快照持久化与 Session Attach 恢复实施方案

> 状态：待实施  
> 日期：2026-07-28  
> 涉及仓库：`ftre`、`ftre-desktop`  
> 兼容策略：测试阶段破坏性升级；可删除 `C:\Users\蒋全明\.ftre\sessions`，不迁移旧 Event 回放数据。

## 1. 原始需求与问题背景

用户在一个长任务执行中切换到另一个 session，稍后再切回原 session 时，客户端
需要恢复该 session 的进行中状态。当前恢复流程是：

```text
Desktop HTTP 拉取已持久化 Msg 历史
  -> Desktop 发送 WebSocket attach
  -> Gateway 回放 attach 前积累的流式 Event
```

当前 `Turn.reply_messages[reply_id]` 会持续聚合 `Msg`，但只在 `REPLY_END`
才调用 `SessionManager.save_message()`。因此未完成的长 Reply 没有可持久化快照，
Gateway 只能在 `_VolatileReplayBuffer` 中缓存大量 `TEXT_BLOCK_DELTA`、
`TOOL_RESULT_TEXT_DELTA` 等 Event 并在 attach 时重放。

这会造成：

- 长文本、长工具输出会产生数万至数十万条 Event；
- attach 网络包和客户端 reducer 工作量与任务长度线性增长；
- session 切换恢复依赖 Gateway 内存，Gateway 重启时看不到进行中的内容；
- 磁盘历史在 Reply 结束前不完整；
- `_VolatileReplayBuffer` 当前的 `deque()` 未设置 `maxlen`，实际不存在容量上限。

本方案确认：**Event 是实时增量协议，不是持久化或历史恢复协议。**
恢复的基本单位必须是完整 `Msg` 快照。

---

## 2. 最终目标

```text
持久化层：完成 Msg + 进行中 Msg 的最新快照
实时层：当前连接上的后续 AgentStreamEvent
attach：当前进行中 Msg 快照 + 此后的实时 Event
```

完成后必须满足：

1. `
` 后立刻有一条 assistant Msg 出现在 `state.json`；
2. 切换/重连 session 时不回放历史 delta；
3. attach 的恢复成本与“当前有几个未完成 Reply”相关，而不是与已产生 Event 数量相关；
4. Gateway 重启后仍可从 state.json 展示最后一次 checkpoint 的进行中内容；
5. 同一个 Reply 始终使用同一个 `reply_id`/`Msg.id`，最终完成只更新该 Msg；
6. `Msg` 继续是唯一持久化消息模型，不新增 Event 数据库或 Event JSON 文件；
7. 不改变上一份 Token Usage 设计：`Msg.token.usage` / `last_call_usage` 随快照一起更新。

---

## 3. 状态模型

### 3.1 进行中 Msg

不新增独立的“任务消息”实体。进行中的 assistant Msg 使用已有字段表达：

```json
{
  "id": "reply_abc",
  "name": "default",
  "role": "assistant",
  "content": [
    {"type": "text", "id": "text_1", "text": "已经生成的内容"}
  ],
  "token": {
    "usage": {
      "prompt_tokens": 1200,
      "completion_tokens": 240,
      "total_tokens": 1440
    },
    "last_call_usage": {
      "prompt_tokens": 1200,
      "completion_tokens": 240,
      "total_tokens": 1440
    }
  },
  "created_at": "2026-07-28T18:00:00+08:00",
  "finished_at": null,
  "finished_reason": null,
  "error": null
}
```

判定规则：

```text
role == "assistant" && finished_at == null
    => 进行中 Reply（open reply）
```

`REPLY_END`、取消或异常收尾后，写入 `finished_at` 和 `finished_reason`，该 Msg
成为普通已完成历史消息。

不增加 `streaming: true` 持久化字段；这是客户端展示状态，可由上述字段推导。

### 3.2 Msg 生命周期

```text
REPLY_START
  -> 创建 AssistantMsg(id=reply_id, content=[])
  -> save_message()                         # 立即插入一次
  -> ActiveReplyRegistry 注册内存快照

AgentStreamEvent
  -> message.append_event(event)            # 内存聚合
  -> 标记 dirty
  -> 按 checkpoint 策略 update_message()

REPLY_END
  -> message.append_event(event)
  -> update_message()                       # 强制最终写入，不再 save_message()
  -> ActiveReplyRegistry 删除

取消 / 异常
  -> 写入 interrupted/error 终态
  -> update_message()                       # 强制最终写入
  -> ActiveReplyRegistry 删除
```

### 3.3 持久化边界

`state.json.messages[]` 同时保存完成 Msg 与进行中 Msg。历史 HTTP 接口返回这两类
快照，不返回 AgentStreamEvent。

Gateway 重启时，若读取到 `finished_at == null` 的 assistant Msg，不能假装它仍在生成：

- 启动恢复阶段将其标为 `interrupted`，并 `update_message()`；或
- 在读取响应中明确给客户端 `running=false`、`finished_reason=interrupted`。

推荐第一种：启动后一次性修复所有遗留 open reply，使 state.json 本身自洽。

---

## 4. Checkpoint 写入策略

### 4.1 原则

不能每个 delta 写一次 JSON，也不能只等 `REPLY_END`。采用：

```text
语义边界 Event       => 立即 checkpoint
高频 delta Event      => 内存聚合 + 节流 checkpoint
结束/取消/异常        => 强制最终 checkpoint
```

### 4.2 必须立即 checkpoint 的事件

| 事件 | 原因 |
|---|---|
| `REPLY_START` | 使 Reply 立刻成为可恢复 Msg |
| `TEXT_BLOCK_END` | 已完成文本 block 是稳定语义单元 |
| `THINKING_BLOCK_END` | 已完成思考 block 是稳定语义单元 |
| `DATA_BLOCK_END` | 数据块完整后才能可靠恢复 |
| `TOOL_CALL_START` | 客户端切回后可显示正在调用的工具 |
| `TOOL_CALL_END` | 工具参数已经完整 |
| `TOOL_RESULT_END` | 工具执行结果是关键状态 |
| `MODEL_CALL_END` | token 用量与调用状态稳定 |
| `REPLY_END` | 最终完成状态 |
| cancel/error/interrupted | 防止丢失已可见的部分内容 |

### 4.3 节流 delta checkpoint

以下事件只改内存快照：

```text
TEXT_BLOCK_DELTA
THINKING_BLOCK_DELTA
DATA_BLOCK_DELTA
TOOL_CALL_DELTA
TOOL_RESULT_TEXT_DELTA
TOOL_RESULT_DATA_DELTA
```

建议初始参数：

```python
CHECKPOINT_INTERVAL_SECONDS = 0.5
CHECKPOINT_TEXT_DELTA_BYTES = 8 * 1024
```

满足下列任意一条时提交最新快照：

1. 距离上次成功 checkpoint >= 500ms；
2. 未持久化文本/数据累计 >= 8KB；
3. 收到第 4.2 节语义边界 Event；
4. Reply 结束、取消或异常。

实现必须使用“latest wins”而不是每次 delta 创建新任务：

```text
dirty Msg A -> 若写入任务已存在，只更新 latest_snapshot
写入完成   -> 若期间仍 dirty，再写一次最新 snapshot
```

这样慢磁盘只会落后一个最新快照，不会积压数万个 JSON 写任务。

---

## 5. Gateway 后端实施

### 5.1 修改范围

主要文件：

```text
E:\ftre\src\ftre\agent\turn_executor.py
E:\ftre\src\ftre\session\manager.py
E:\ftre\src\ftre\channel\ws_channel.py
E:\ftre\src\ftre\api\routes.py             # 如需补充运行态字段
E:\ftre\tests\test_*.py
```

`SessionManager.update_message()` 已存在，能按 `Msg.id` 原地替换消息数组中的快照。
不需要新增数据库表。

### 5.2 ActiveReplyRegistry

在 Gateway 进程内新增一个小型运行态注册表，建议由 `TurnExecutor` 所属的
`AgentLoop` 持有：

```python
@dataclass
class ActiveReply:
    session_id: str
    reply_id: str
    message: Msg
    revision: int
    dirty: bool
    last_checkpoint_at: float
    checkpoint_task: asyncio.Task | None

class ActiveReplyRegistry:
    async def begin(...)
    async def apply_event(...)
    async def snapshot(session_id) -> list[ActiveReplySnapshot]
    async def finish(reply_id)
```

职责：

- 保存每个 session 当前 open reply 的最新内存 Msg；
- 统一 checkpoint 节流；
- 为 attach 返回当前快照；
- 管理单调递增 `revision`；
- 不保存 Event 列表。

`Turn.reply_messages` 可以被该 registry 取代，或保留为同一个 Msg 对象的引用；
禁止形成两份互相独立的聚合状态。

### 5.3 TurnExecutor 改造

现有 `publish_agent_event()` 在 `ReplyEndEvent` 时才 `save_message()`。改为：

1. 首个携带 `reply_id` 的 `REPLY_START` 创建 `AssistantMsg`；
2. 调用 `save_message(session_id, message)`；
3. 所有同 `reply_id` Event 调用 `message.append_event(event)`；
4. 把最新快照交给 registry，按第 4 节 checkpoint；
5. `ReplyEndEvent` 后强制 `update_message(message)`；
6. `_persist_open_replies()` 改为更新已经存在的 Msg，绝不能再次 `save_message()`。

注意：当前 `REPLY_START` 本身会由 `Msg.append_event()` 填充 name/role，但不能等
事件派发后才存空消息；应在聚合前确保 `AssistantMsg(id=reply_id)` 已插入。

### 5.4 Attach 快照协议

新增下行 WS 帧，不属于 `AgentStreamEvent`：

```json
{
  "frame_id": "sync_xxx",
  "type": "reply_snapshot",
  "data": {
    "session_id": "ws_sess_xxx",
    "replies": [
      {
        "reply_id": "reply_abc",
        "revision": 184,
        "message": {"id": "reply_abc", "role": "assistant", "...": "完整 Msg"}
      }
    ]
  }
}
```

通常一个 session 同时最多一个 open reply，但协议使用数组，避免给未来并行子任务
或多回复模型增加破坏性改动。

attach 流程必须保证快照先于后续实时 Event 到达同一 ws：

```text
获取 session output lock
  1. 从 ActiveReplyRegistry 取得最新 snapshots
  2. 将 reply_snapshot 放入该 ws 的 FIFO 发送队列
  3. 将 ws 加入该 session 的订阅集合
释放 lock
  4. 后续 Event 只能排在 snapshot 后
```

当前 `WebSocketChannel.send()` 直接并发 `send_text()`，实施时需要为每个 ws 增加
发送队列/锁，或以 session output lock 串行化 snapshot 与 outbound Event。不能先
注册连接再裸发 snapshot，否则新的 Event 可能先到，导致客户端把旧快照覆盖新 delta。

### 5.5 删除旧 delta replay

删除以下职责：

```text
_VolatileReplayBuffer 缓存 TEXT_BLOCK_DELTA 等 Event
attach 时 replay(session_id, ws)
REPLY_END 时清理历史 delta 的逻辑
```

保留正常 live outbound 分发。attach 时仅发送 `reply_snapshot`；没有进行中 Reply
则发送空 `replies` 或不发送该帧（二选一，推荐总是发送，客户端逻辑更确定）。

不需要 Event cursor、`after_seq` 或 Event 数据库存储。`revision` 只用于客户端
拒绝比已应用快照更旧的 `reply_snapshot`，不是 replay cursor。

### 5.6 Gateway 重启与异常处理

启动后扫描 `finished_at is null` 的 assistant Msg：

```python
message.finished_at = now_iso()
message.finished_reason = ReplyFinishedReason.INTERRUPTED
message.error = {
    "code": "gateway_restarted",
    "message": "Gateway restarted before this reply completed.",
}
await session_manager.update_message(message)
```

不能在 Gateway 重启后把旧 Msg 标为 `streaming`，因为 LLM 调用并不能恢复。

---

## 6. Desktop 客户端实施

### 6.1 修改范围

```text
E:\binn\ftre-desktop\packages\renderer\src\services\websocket-client.ts
E:\binn\ftre-desktop\packages\renderer\src\stores\chat.ts
E:\binn\ftre-desktop\packages\renderer\src\stores\session.ts
E:\binn\ftre-desktop\packages\renderer\src\services\api.ts
E:\binn\ftre-desktop\packages\renderer\src\stores\*.test.ts
```

### 6.2 HTTP 历史恢复

`persistedMessageToChat()` 对 assistant Msg 使用：

```typescript
const isOpenReply =
  record.role === "assistant" && record.finished_at == null;

return {
  id: record.id,
  role: "assistant",
  // ...content / blocks / toolResults / token...
  streaming: isOpenReply,
};
```

这允许用户切回 session 时先显示最近一次 checkpoint，而不是空白等待 WS。

### 6.3 `reply_snapshot` reducer

`WebSocketClient` 保持把未知 server frame 交给 store；在 `chat.ts` 增加
`applyReplySnapshot(bucket, payload)`：

1. 将 `message` 用与历史恢复相同的转换函数转成 `ChatMessage`；
2. 按 `message.id === reply_id` 查找；
3. 已存在则 replace，不能 append；
4. 不存在则按 created_at 顺序插入；
5. `streaming=true`；
6. 记录 `replyId -> revision`；收到 revision 更小的 snapshot 时丢弃；
7. 写入 `seenEventIds` 的边界不变。

不要把 snapshot 再拆成伪造的 `TEXT_BLOCK_START/DELTA`；它本身就是 Msg。

### 6.4 Event reducer：按 reply_id 定位

当前多个分支依赖 `replaceTail()`。改造为：

```typescript
ensureReply(replyId): ChatMessage
replaceReply(replyId, updater): void
```

规则：

```text
找到 id == reply_id 的 assistant Msg -> 在该 Msg 上应用 Event
找不到                           -> 创建 streaming Msg
```

`REPLY_END` 也必须按 `reply_id` 关闭对应 Msg，而不是关闭数组末尾消息。
这同时修复切换后 history 中已有 running Msg 时的续接问题。

### 6.5 Session 切换顺序

保持当前顺序，但语义改变：

```text
1. HTTP 读取 completed + open Msg snapshots
2. loadSessionMessages()
3. WS subscribeOnly(sessionId)
4. 收到 reply_snapshot 时按 id 覆盖为最新状态
5. 接收其后的 live Event
```

不再等待或处理 delta replay。HTTP 结果略旧并不构成问题，因为 attach snapshot
会用完整最新 Msg 覆盖它。

### 6.6 UI 行为

- 进行中 Msg 显示为 streaming；
- 进行中的 tool call / tool result 基于 snapshot block 原样渲染；
- Token 用量直接读取 `message.token`；
- Gateway restart 后收到 interrupted Msg，停止 loading，展示可见正文和中断状态；
- 不新增 Event 历史面板的数据源；trace 仍可以使用已有 trace 系统。

---

## 7. API 与类型定义

### 7.1 HTTP `SessionMessage`

不需要为进行中 Msg 增加新字段。现有字段已足够：

```typescript
finished_at: string | null;
finished_reason: string | null;
error: Record<string, unknown> | null;
```

如当前 API 对 `finished_at` 使用空字符串等非 `null` 值，先统一为 JSON `null`。

### 7.2 WebSocket 类型

在 `ServerMessage` 联合类型中加入：

```typescript
interface ReplySnapshotFrame {
  type: "reply_snapshot";
  frame_id: string;
  data: {
    session_id: string;
    replies: Array<{
      reply_id: string;
      revision: number;
      message: SessionMessage;
    }>;
  };
}
```

`reply_snapshot` 与 `agent_event` 分开处理。不能伪装成 `CUSTOM` Event，避免污染
Core 的 AgentStreamEvent 协议。

---

## 8. 测试计划

### 8.1 Gateway 单元测试

1. `REPLY_START` 后立即有一条 open assistant Msg；
2. 多个 `TEXT_BLOCK_DELTA` 在 500ms 内只触发一次延迟 checkpoint；
3. `TEXT_BLOCK_END` 无视节流立即 update；
4. `TOOL_CALL_START`、`TOOL_RESULT_END`、`MODEL_CALL_END` 均立即 update；
5. `REPLY_END` 更新原 Msg，不产生重复 id；
6. cancel/error 更新原 Msg 并有终态；
7. Gateway 重启修复遗留 open Msg 为 interrupted；
8. attach 有 open reply 时只收到一个 `reply_snapshot`，没有历史 delta；
9. attach 与同时到来的 Event 保证 snapshot 在前；
10. 10 万个 delta 后 replay buffer 内不存 10 万 Event。

### 8.2 Desktop 单元测试

1. HTTP 返回 open assistant Msg 时显示 streaming；
2. `reply_snapshot` 替换已有同 id Msg；
3. 较旧 revision 不覆盖较新快照；
4. snapshot 后 `TEXT_BLOCK_DELTA` 追加到同 reply；
5. `REPLY_END` 关闭指定 reply，不关闭尾部其他消息；
6. session A/B 快速切换时 snapshot 不串 session；
7. 完整 Msg snapshot 恢复 tool call、tool result 和 token 字段。

### 8.3 集成/压测

```text
创建长任务 -> 产生 100,000 个 TEXT_BLOCK_DELTA
中途切到 session B -> 再切回 A
```

验收：

- HTTP 返回一条 open Msg 快照；
- attach 下行 `reply_snapshot` 数量为 1；
- 不发送 100,000 个旧 delta；
- 切回后可继续看到新文本、工具状态与 token；
- Gateway 进程内存不会随“未 attach 时长”无限积累 Event；
- Gateway 重启后可看到最后 checkpoint，且 Reply 标记 interrupted。

---

## 9. 实施顺序

1. 在 `ftre` 为 open Msg 建立 checkpoint 生命周期，并写 Gateway 单测；
2. 增加 `ActiveReplyRegistry` 和 `reply_snapshot` WS 帧；
3. 用 per-session/per-ws 输出顺序保证 attach 无竞态；
4. 删除 `_VolatileReplayBuffer` 的 delta 缓存与 attach replay；
5. 在 Desktop 增加 snapshot 类型、open Msg 历史转换和按 reply_id reducer；
6. 补齐单测、压测和 Gateway restart 测试；
7. 删除测试 session 数据，以新结构从零开始验证。

不要先改客户端再改 Gateway；客户端需要同时兼容 HTTP open Msg 和 `reply_snapshot`
才能避免切换窗口丢失状态。

---

## 10. 明确不做的事

- 不把 `TEXT_BLOCK_DELTA` 等 AgentStreamEvent 写入 state.json；
- 不建立 Event SQL 表、事件日志文件或 Event 压缩存储；
- 不把历史 Event 回放作为 attach 恢复协议；
- 不迁移或兼容旧 session 的 Event 数据；
- 不使用 `reply_snapshot` 代替实时 Event；它只用于 attach/reconnect 同步；
- 不让客户端根据相邻事件推导完整 Msg，完整 Msg 必须由后端聚合。

## 11. 验收结论

当一个运行 30 分钟、产生海量 delta 的任务切换回来时，恢复网络成本应近似为：

```text
最近 N 轮 Msg HTTP 响应
  + 1 个 reply_snapshot
  + attach 后新增的实时 Event
```

而不是：

```text
最近 N 轮 Msg HTTP 响应
  + 从 REPLY_START 至当前的全部 Event
```

这就是该方案的核心验收标准。
