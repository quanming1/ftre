# Volatile Replay uses event_id

**Goal:** 删除旧的 replay 序号 metadata，统一使用 `AgentEvent.event_id` 去重 HTTP history、WS live 和 WS replay。

**Architecture:**

- `ftre-agent-core` 在 `AgentEvent` 基类生成 `event_id`，`to_dict()` 输出 `{type, event_id, data}`。
- `ftre` 入库时把同一个 `event_id` 写入 `messages.data.event_id`；旧库启动迁移用 `messages.id` 回填。
- `WebSocketChannel` replay buffer 只保存标准下行帧，不再写 replay 序号 metadata。
- replay buffer 可以短暂保留刚入库的稳定事件（如 `assistant_message_complete` / `tool_call` / `tool_result`），覆盖 HTTP fetch 与 WS attach 之间的 race。
- `ftre-desktop` 在 chat reducer 层按 `event_id` 去重，WebSocketClient 不再做 metadata 级去重。

**Dedup key:**

```ts
event.event_id ?? event.data?.event_id ?? frame.id
```

`event_id` 是首选。`frame.id` 只作为旧事件 fallback。
