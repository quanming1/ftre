# PRD-F44 会话快照协议与流式消息持久化收敛

状态：已验收（统一 seq attach 协议）

## 1. 背景与问题

F41/F42/F43 已把 Agent Event、Msg 派生和 WebSocket 下行帧统一起来，但当前
SessionService 仍把每一个流式 `assistant/chunk` 写入 `session.jsonl`，并在
`turn/end` 再写一份完整 `assistant/message`。因此一次用户输入会同时产生：

- 数千条 thinking/text 增量事件；
- 工具调用、工具结果和生命周期事件；
- 一份包含所有内容的完整 Assistant Msg；
- `session.json` 元信息和 `session.jsonl` 事件日志两份持久化文件。

真实会话 `ws_sess_1db191b4521a` 的证据是：778 个物理 JSONL 行、最高 seq 8464、
7,429 个 thinking 增量、876 个 text 增量、36 次工具调用，最终 Assistant Msg
约 326KB；单次最后 LLM 调用输入约 89K token，整轮累计 usage 1.57M，后者是
24 轮调用的累计值而不是一次超过 1M 的上下文。当前格式把“直播传输”和“恢复存储”
混成了同一条日志，放大了文件和恢复复杂度。

## 2. 目标

1. 一个 Session 目录只保留一个 `session.json`；Inbox 自己的 `inbox.json` 仍是
   独立队列文件，不属于 Session transcript。
2. `assistant/chunk` 只用于当前进程的直播和内存聚合，不作为独立记录落盘。
3. 以完整 Msg 快照作为恢复事实：在固定时间窗口或语义边界原子写入一次；每条
   Msg 保存最后修改它的 Event.seq。
4. 刷新/重连后仍能看到完整的用户消息、Assistant 文本/思考、工具调用结果、
   压缩摘要、权限状态和插入消息。
5. Event → Msg 的关系明确：Event 是运行时增量协议，Msg 是持久化/HTTP 恢复协议；
   Event 与 Msg 统一使用 `seq`，WebSocket attach 直接返回 HTTP 基线之后尚未
   折叠的 Event 数组，不再维护第二套 Snapshot/游标协议。
6. 兼容已有 `session.jsonl`：首次访问时转换为 Msg 快照，成功后删除旧 JSONL；
   新代码不再写入或依赖旧 JSONL。
7. 保留扩展能力：未知 Msg block 统一包进 `extension` 保留容器（其中的 `data`
   保存原始 JSON），顶层 `extensions` 原样保留；新增 live Event 不要求修改快照
   格式，只要能在语义边界生成当前 Msg。

## 3. 非目标

- 不把 Event、Msg、Tool、Inbox、Agent Runtime 合并成一个 Service。
- 不把每个 chunk 做成数据库行、压缩行或新的持久化子协议。
- 不让客户端重新实现服务端的历史持久化；客户端只消费 HTTP Msg 和 attach/live Event。
- 不改变 Inbox 的队列消费策略、Agent 的重试策略或 LLM provider 协议。

## 4. 术语与边界

```text
Agent Runtime
    └─ append Event 到 SessionLog（内存；chunk 也在这里）

SessionService
    ├─ derive Event → Msg（当前进程）
    ├─ SnapshotCoordinator（定时/语义 checkpoint）
    └─ 原子写 session.json（唯一 Session 持久化文件）

WebSocket Channel
    ├─ session/subscribed：attach 返回 seq 之后的 Event[]
    └─ session/event：后续直播 Event，含 seq

HTTP /messages
    └─ 返回 Msg[] + 本次快照覆盖的 seq
```

`SessionLog` 是运行时事件总线，不是磁盘格式 Owner。`SnapshotCoordinator` 是
唯一的 Session 快照写入 Owner。Repository 只负责原子读写和 Session 元信息索引；
它不再拥有第二套消息存储。

## 5. 单文件数据模型

路径：`~/.ftre/sessions/<session_id>/session.json`。

```json
{
  "schema_version": 5,
  "session": {
    "id": "ws_sess_xxx",
    "agent_id": "default",
    "channel_id": "ws",
    "title": "",
    "workspace": "E:/ftre",
    "created_at": "2026-09-07T10:00:00+08:00",
    "updated_at": "2026-09-07T10:01:00+08:00",
    "last_user_text": "最后一条用户消息"
  },
  "metadata": {},
  "seq": 42,
  "messages": [
    {
      "id": "user_xxx",
      "name": "default",
      "role": "user",
      "seq": 41,
      "content": [{"type": "text", "text": "你好"}],
      "metadata": {"request_id": "req_xxx"},
      "created_at": "2026-09-07T10:00:00+08:00"
    }
  ],
  "requests": {
    "req_xxx": {
      "message_id": "user_xxx",
      "run_id": "run_xxx",
      "status": "completed",
      "fingerprint": "..."
    }
  },
  "extensions": {}
}
```

字段约束：

| 字段 | 说明 |
| --- | --- |
| `schema_version` | 当前为 5；结构变化才递增，普通新增 Msg block 不递增 |
| `seq` | Session 的持久单调事件水位；快照覆盖到该序号，重启后继续递增 |
| `messages` | 完整、可直接渲染和转换给 LLM 的 Msg 快照；每条 Msg 的 `seq` 是最后修改它的 Event.seq |
| `requests` | `request_id → run_id/message_id/status/fingerprint` 幂等索引 |
| `extensions` | 插件命名空间数据，默认空对象，未知字段不丢弃 |

`session.json` 是一个原子替换单元：先写同目录随机临时文件、flush + fsync，成功后
`os.replace`。写失败时内存状态不回退、不报告成功；旧文件保持可读。

## 6. Event 与 Msg 协议

### 6.1 Live Event

- `assistant/chunk`、`tool/result-start` 等运行时增量仍通过 `session/event` 下行，
  只保存在当前进程的 `SessionLog` 和客户端内存中。
- `assistant/message`、`tool/result`、`compact/message`、`turn/end` 等语义边界
  触发快照 checkpoint，但仍先正常派发 Event。
- Event 的 `seq` 是 Session 级持久单调序号；进程重启后从 `session.json.seq + 1`
  继续，禁止重新从 0 开始。

### 6.2 Durable Msg

- 快照保存 `Msg.model_dump(mode="json")`，不是 chunk 数组，也不保存 provider 的
  中间流对象。
- 快照读取遇到未知内容块时，`Msg` 将其规范化为 `type="extension"`，并在
  `original_type`/`data` 中保留原始判别值和 JSON；客户端可以忽略该块，但不能
  丢掉同一条 Msg 或阻断整个 Session 恢复。
- 当前进程读取时，SessionService 将快照 Msg 与尚未 checkpoint 的 live Event fold
  合并；客户端收到 `/messages` 时直接以 Msg 初始化 assembler。
- `request_id`、`run_id`、工具状态、压缩摘要和回滚后的最终消息都属于 Msg/metadata，
  在 checkpoint 时一起原子提交。

### 6.3 Msg.seq 合并规则

- 一个 Msg 可以由多个 Event 共同构成；同一个 `message_id` 的 Event 始终更新同一条 Msg。
- `Msg.seq` 等于最后一个实际修改该 Msg 的 Event.seq，而不是 chunk 数量，也不是
  Event.seq 的数组。
- 例如 `assistant/chunk(101)`、`assistant/chunk(102)`、`turn/end(103)` 最终得到
  `Msg.seq = 103`；重复或更旧事件不得回退 Msg.seq。
- HTTP 响应顶层也使用字段名 `seq`，表示整个 Msg 快照覆盖的全局事件水位。它不能
  用 `max(messages[].seq)` 替代，因为 `turn/start`、`session/status` 等事件可能不产出 Msg。

### 6.4 Attach 恢复帧

客户端先通过 HTTP 建立 Msg 基线：

```json
// GET /api/sessions/ws_sess_xxx/messages?limit_turns=5
{
  "seq": 100,
  "messages": [/* Msg[]，每条 Msg 含 seq */],
  "status": "running",
  "queue": {/* Inbox 快照 */}
}
```

然后发送 attach：

```json
{
  "type": "attach",
  "payload": {"session_id": "ws_sess_xxx", "seq": 100}
}
```

attach 的下行响应仍使用 `session/subscribed`，但直接携带未折叠 Event：

```json
{
  "v": 1,
  "session_id": "ws_sess_xxx",
  "type": "session/subscribed",
  "payload": {
    "seq": 102,
    "events": [
      {"type": "assistant/chunk", "seq": 101, "message_id": "a1", "data": {}},
      {"type": "assistant/chunk", "seq": 102, "message_id": "a1", "data": {}}
    ],
    "status": "running",
    "has_more": false,
    "resync_required": false
  }
}
```

`seq` 是服务端当前水位；`events` 只包含客户端请求 seq 之后的事件。
如果客户端 seq 早于已持久化快照或领先服务端，返回 `resync_required=true`，客户端
重新请求 HTTP `/messages`，不再发送第二种 Snapshot 帧，也不再提供 `/events?after_seq=`。

## 7. Snapshot 写入策略

默认 `snapshot_interval_ms=500`，由 `SnapshotConfig` 配置，可通过 Host 配置覆盖。

1. 第一个 live Event 到达时启动固定窗口计时器；窗口内后续 chunk 不重置计时器。
2. 以下语义事件立即 checkpoint：`user/message`、`assistant/message`、`tool/result`、
   `compact/message`、`turn/end`、`hint/message`、`approval/asked`。
3. 定时或立即 checkpoint 都读取同一个 Session Msg 快照，生成 `messages`、`requests`、
   `seq`，再调用 Repository 原子写入。
4. `flush_log()` 是显式屏障：等待该 Session 的快照写入完成；Gateway close 时对所有
   dirty Session 做有界 drain。
5. 写入失败保留 dirty 状态并记录日志，下一次 checkpoint 重试；不会删除内存事件。

## 8. 恢复与迁移

### 8.1 新格式恢复

启动扫描只读取 `session.json`。首次打开 Session 时加载 `messages`、`requests`、
`seq`；创建从 `session.json.seq + 1` 开始的 live SessionLog。客户端先通过 HTTP
`/messages` 建立 Msg 基线，再通过 attach 获取该 seq 之后尚未折叠的 Event；新事件
继续使用同一条 Session seq，不存在跨进程归零。

### 8.2 F43 JSONL 一次性迁移

若目录存在旧 `session.jsonl` 且 `session.json` 没有 `messages`：

1. 读取旧 JSONL，展开历史 chunk row，修复末尾未闭合 turn；
2. 使用同一 `derive_messages` 生成 Msg[]；
3. 写入 `schema_version=5` 的 `session.json`；
4. 原子写成功后删除 `session.jsonl`；失败则保留旧文件并报告迁移错误；
5. 迁移代码只用于一次性导入，不再提供 JSONL append/write-behind Owner。

旧数据中的工具、压缩、用户消息和未完成 Assistant 都必须在迁移后的 Msg 快照中可见；
迁移过程不能因为未知 UI part 丢掉同一条用户消息。

## 9. 前后端修改范围

### 后端

- `packages/ftre-agent/src/ftre_agent/session/`：保留 Event/Msg/derive/SessionLog
  运行时契约，补充快照基线所需的纯数据 helper。
- `src/ftre/services/session/entity/state.py`：SessionMetaFile 改为 schema v5 全量
  Snapshot 文档模型。
- `src/ftre/services/session/persistence/snapshot.py`：原子快照读写、固定窗口协调器、
  旧 JSONL 一次性导入。
- `src/ftre/services/session/service.py`：删除 JSONL write-behind/repair Owner，维护
  baseline Msg + live Event、checkpoint、seq、request index、fork。
- `src/ftre/services/session/persistence/repository.py` 与 `json_store.py`：只负责
  单文件 SessionSnapshot CRUD，所有 metadata 更新保留 messages。
- `src/ftre/services/messaging/wire.py`、WebSocket Channel、Session Router：统一
  `seq` 字段；`session/subscribed` 携带 attach Event[]，删除 `session/snapshot`、
  `snapshot_revision`、`last_seq` 和 `/events?after_seq=`。
- 删除旧生产路径：`persistence/jsonl.py`、`persistence/chunk_rows.py`、`repair.py`。

### 客户端

- `types/wire.gen.ts`、`websocket-client.ts`：生成统一 `seq` 和 attach Event[] 契约。
- `sessionEventClient.ts`：消费 attach Event[]；短断线和跳号都重新 attach，水位不可
  追平时重新 hydrate HTTP Msg。
- `clientSessionProjection.ts`、`chatProjection.ts`、`chat.ts`：以 HTTP Msg 作为
  基线，attach/live chunk 继续即时 fold，不增加第二套 Snapshot 转换器。
- 其他消息 UI 不改变，不增加第二套 Event→Msg 转换器。

## 10. 分阶段实施与验收

### P0：契约与基线

- 更新本 PRD、F41/F42/F43 交叉引用和 TODO；冻结 schema v5、统一 seq attach 帧和迁移规则。
- 添加真实会话 fixture：包含 user、thinking/text chunk、tool、compact、approval、
  interrupted turn。
- 验收：fixture 可被解析，明确区分 `last_call_usage` 与整轮累计 usage。

### P1：后端单文件 Snapshot

- 实现 `SessionSnapshotFile`、原子写入、固定窗口/语义 checkpoint。
- `SessionService` 读写全量 Msg；fork、state page、token usage、request 幂等改为从
  Snapshot + live Event 读取。
- 验收：新 Session 目录只有 `session.json`；chunk 不写成记录；强制 flush/close 后
  JSON 可解析，写失败旧文件不损坏。

### P2：恢复与 WebSocket

- 实现旧 JSONL 一次迁移；删除旧生产 append/repair 模块。
- attach 返回基线之后 Event[]，统一 `seq`；不可追平时只返回 `resync_required`，由
  客户端重新请求 HTTP `/messages`。
- 验收：运行中刷新、断线、Gateway 重启后消息/工具/压缩内容完全一致；新 seq 不归零；
  无 JSONL 回读、无第二种 Snapshot 帧。

### P3：客户端与插件回归

- 客户端 hydrate/assembler 接入 HTTP Msg + attach Event[]；保留 live Event 的低延迟渲染。
- 验证 Inbox 插入用户消息、Tool 权限、Compaction、Rollback、未知扩展字段。
- 验收：同一 Session 运行中和重启后 UI Msg 数量、内容、状态一致；不会重复用户消息，
  不会因为旧 seq 跳过 Assistant 文本。

### P4：收尾与交付

- 删除旧 JSONL/chunk/repair 引用、测试、文档和空目录；静态扫描确保唯一持久化 Owner。
- 运行后端 pytest/Ruff/diff check、客户端测试/typecheck；构建 Windows portable 包并
  启动验证。
- 验收：新会话目录单文件；发布包可启动、恢复和发送消息。

## 11. 风险与控制

| 风险 | 控制 |
| --- | --- |
| 快照写入期间进程崩溃 | 临时文件 + fsync + replace；旧快照保持可用 |
| 流式内容尚未到 checkpoint | close/turn end 强制 flush；客户端仍保留当前内存流 |
| 旧 JSONL 中段损坏 | 仅允许末尾修复；中段损坏报告错误，不静默丢历史 |
| 客户端水位不可追平 | attach 返回 `resync_required`，客户端重新 hydrate HTTP Msg |
| 插件新增 block/event | Msg content/extension 开放；live 未知事件按现有忽略策略处理 |
| token 统计误判 | API usage 记录按“单次 last_call / 整轮累计 / 当前 context”分开，压缩只读 context |

## 12. 完成标准

- Session transcript 只有 `session.json`；不再创建 `session.jsonl`。
- 文件中不存在 `thinking-chunks`、`text-chunks`、`assistant/chunk` 独立持久化记录。
- Event→Msg、Msg→HTTP/WS Snapshot 的代码入口唯一、可读、可测试。
- 重启恢复覆盖文本、思考、Tool、权限、Compaction、插入 User、Rollback 和幂等 request。
- 真实会话复现：单轮 24 次调用的累计 usage 不被误报成一次上下文超限。
- 全量测试、静态检查、客户端构建和 portable 启动均通过。

## 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-09-07 | 新建 F44；根据 `ws_sess_1db191b4521a` 真实数据将 F43 的 JSONL/chunk 持久化改为单文件 Msg Snapshot，并定义跨进程恢复协议 |
| 2026-09-07 | 完成 schema v5 Snapshot、旧 JSONL 一次迁移、客户端 hydrate、跨重启 Msg/request 幂等和未知 Msg block 保留 |
| 2026-09-07 | 修订为 schema v5 统一 seq：Msg 保存最后修改事件序号，HTTP 建立 Msg 基线，WS attach 返回未折叠 Event[]；删除 revision/cursor/last_seq、session/snapshot 和 /events 补齐路径 |
| 2026-09-07 | 验收完成：后端 796 项测试、客户端 597 项测试、TypeScript、Ruff、wire 生成物稳定性和 Windows portable 启动健康检查通过 |
| 2026-09-07 | 修复历史 Msg 回灌为 Provider 字典后被 Runtime 误判为本轮新 Assistant 的问题：Assistant 基线改在历史回灌后记录，并增加回归测试；清理真实会话 `ws_sess_72d9f3b44346` 的 4 条重复快照 |
