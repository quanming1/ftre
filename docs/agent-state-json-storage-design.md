# FTRE Agent State JSON 持久化设计

> 状态：设计稿，尚未实施  
> 目标读者：负责实施 FTRE Session 持久化重构的开发者  
> 适用仓库：`E:\ftre`，并需联动验证 `E:\binn\ftre-desktop`

## 1. 背景

FTRE 当前使用 `~/.ftre/sessions.db` 保存三类数据：

| 表 | 内容 |
|---|---|
| `sessions` | Session 元信息 |
| `messages` | 完整 `Msg` 快照，但 `content`、`metadata`、`usage` 等字段以 JSON 字符串存入 SQL 列 |
| `external_sessions` | 外部 Channel 会话到本地 Session 的映射 |

消息协议已经完成从“持久化 Event”到“持久化完整 Msg”的切换：

```text
AgentStreamEvent
    ↓ 仅用于实时 WebSocket、内存聚合和 trace
Msg.append_event()
    ↓
完整 UserMsg / AssistantMsg / SystemMsg
    ↓
持久化
```

因此，当前 SQLite 的主要作用已经退化成：

1. 保存一组 JSON 文档；
2. 按 `session_id` 找到一份 Session；
3. 列出 Session；
4. 保存和读取一个 Session 的 Msg 列表。

本次设计决定不引入 TinyDB，也不继续使用 SQLite 保存这些 JSON 文档，而是使用：

```text
Pydantic AgentStateFile
+ 每个 Session 一份 state.json
+ per-session asyncio.Lock
+ 临时文件 + os.replace 原子写入
```

## 2. 调研结论与设计决策

### 2.1 QwenPaw / AgentScope 的启发

QwenPaw 的主会话文件保存完整 AgentScope `AgentState`：

```text
AgentState
├── context: list[Msg]
├── summary
├── reply_context
├── permission_context
├── tool_context
├── tasks_context
└── middle_context
```

其中 `context` 是完整 Msg，不保存 `TEXT_BLOCK_DELTA` 等流式 Event。

QwenPaw 同时还有独立的 `history.db`，用于保存被 Scroll compact 移出活动上下文的长期历史。因此不能直接复制其“只保存活动 context”的做法：FTRE Desktop 需要随时读取完整会话历史。

FTRE 必须明确区分：

```text
messages     完整、可展示的 Session 消息历史
summary      给模型构建上下文时使用的滚动摘要
Event        运行过程，不持久化
```

### 2.2 不使用 TinyDB

FTRE 的主要访问模式是按 `session_id` 整体读写一份状态，不需要 TinyDB 的 Table、Document ID 和查询表达式。

如果所有 Session 放入同一个 TinyDB JSON：

- 每次消息写入可能重写整个数据库文档；
- 单文件损坏会影响所有 Session；
- 仍然需要自行实现 Pydantic 校验、Schema 迁移、异步锁和安全写入。

如果一个 Session 使用一个 TinyDB：

- TinyDB 的查询和 Table 能力基本没有价值；
- 最终仍然是“一份 JSON 文件”，不如直接使用 Pydantic。

### 2.3 保留 `schema_version`

持久化根对象必须包含：

```json
{
  "schema_version": 1
}
```

它表示磁盘 JSON 的结构版本，不是：

- FTRE 软件版本；
- 文件保存次数；
- 消息数量；
- 并发 revision。

只有当旧文件需要迁移才能由新代码理解时，才增加 `schema_version`。

加载规则：

```python
if schema_version == 1:
    return AgentStateFileV1.model_validate(data)
if schema_version == 0:
    return migrate_v0_to_v1(data)
raise UnsupportedAgentStateVersion(schema_version)
```

未知的更高版本必须拒绝写入，防止旧程序覆盖新格式数据。

## 3. 目标

- 一份 Session 对应一份人类可读、可复制的 JSON 文件；
- 完整持久化 `Msg`，不持久化流式 Event；
- 保持现有 Session API 的对外响应格式；
- Desktop 无需理解磁盘格式；
- compact 后 Desktop 仍可读取完整消息历史；
- 多次 compact 使用滚动摘要，不删除原始 Msg；
- 写入过程具备进程内并发安全和文件替换原子性；
- 从现有 `sessions.db` 无损、可重复地迁移；
- 迁移期间不删除、不覆盖旧数据库；
- 不引入新的第三方存储依赖。

## 4. 非目标

第一版明确不实现：

- 跨 Session 全文搜索；
- Event Store 或 Event Replay；
- TinyDB；
- 多进程同时写同一个配置目录；
- compact 历史版本列表；
- ToolResult 原文与 compact stub 的双层 overlay；
- 独立 `reply_context` / `cur_iter` 崩溃续跑；
- 全局 AgentState revision / 乐观锁；
- 单独的 Session 索引文件；
- 自动删除旧 `sessions.db`；
- 自动创建滚动 `.bak` 文件。

如果未来需要跨 Session 检索，应增加独立、可重建的检索投影，不应污染 `state.json` 的消息语义。

## 5. 最终磁盘结构

默认目录：

```text
~/.ftre/
├── sessions.db                         # 旧库，启动时直接删除（不迁移）
└── sessions/
    ├── ws_sess_ed930104a1d2/           # 目录名即 session_id
    │   └── state.json
    └── octo_sess_def456/
        └── state.json
```

Session ID 格式为 `<channel_id>_sess_<12位hex>`，只允许 `[A-Za-z0-9_-]` 字符，
可直接作为 Windows/Linux 目录名，无需编码。

生成规则：

```python
sid = f"{channel_id}_sess_{uuid.uuid4().hex[:12]}"
```

channel_id 必须匹配 `^[A-Za-z0-9_-]+$`，否则拒绝创建（不静默替换）。

路径解析必须验证 session_id 字符合法且最终路径仍位于 `~/.ftre/sessions/` 内，
删除时不得对未验证路径执行操作。

## 6. 精简 Agent State Schema

### 6.1 顶层结构

只保留五个顶层字段：

```json
{
  "schema_version": 1,
  "session": {},
  "messages": [],
  "summary": null,
  "metadata": {}
}
```

完整示例：

```json
{
  "schema_version": 1,
  "session": {
    "id": "ws::sess_ed930104a1d2",
    "agent_id": "default",
    "channel_id": "ws",
    "title": "Agent State JSON 持久化设计",
    "workspace": "E:\\ftre",
    "created_at": "2026-07-27T18:00:00+08:00",
    "updated_at": "2026-07-27T21:00:00+08:00"
  },
  "messages": [
    {
      "id": "msg_user_001",
      "name": "default",
      "role": "user",
      "content": [
        {
          "type": "text",
          "id": "block_user_001",
          "text": "帮我设计 Agent State JSON Schema",
          "created_at": "2026-07-27T20:00:00+08:00",
          "finished_at": "2026-07-27T20:00:00+08:00"
        }
      ],
      "metadata": {
        "hide": false,
        "agent_id": "default"
      },
      "created_at": "2026-07-27T20:00:00+08:00",
      "usage": null,
      "finished_at": "2026-07-27T20:00:00+08:00",
      "finished_reason": null,
      "structured_output": null,
      "error": null
    },
    {
      "id": "reply_001",
      "name": "default",
      "role": "assistant",
      "content": [
        {
          "type": "text",
          "id": "block_reply_001",
          "text": "建议每个 Session 使用一份 JSON 文件。",
          "created_at": "2026-07-27T20:00:01+08:00",
          "finished_at": "2026-07-27T20:00:03+08:00"
        }
      ],
      "metadata": {},
      "created_at": "2026-07-27T20:00:01+08:00",
      "usage": {
        "input_tokens": 1200,
        "output_tokens": 100
      },
      "finished_at": "2026-07-27T20:00:03+08:00",
      "finished_reason": "completed",
      "structured_output": null,
      "error": null
    }
  ],
  "summary": null,
  "metadata": {
    "plan": null,
    "external": null
  }
}
```

### 6.2 `session`

```text
session
├── id
├── agent_id
├── channel_id
├── title
├── workspace
├── created_at
└── updated_at
```

约束：

- `id`：原始 Session ID；
- `agent_id`：该 Session 默认使用的 Agent，默认 `"default"`；
- `channel_id`：`ws`、`cron`、`octo`、`subagent` 等；
- `title`：允许空字符串；
- `workspace`：允许空字符串；
- `created_at` / `updated_at`：RFC 3339 / ISO 8601 字符串；
- 对外 API 为兼容现有 Desktop，可继续返回 epoch float。

`running` / `idle` 不写入文件，它们是 Gateway 当前进程的运行态。

### 6.3 `messages`

`messages[]` 中每一项必须可以直接通过：

```python
Msg.model_validate(item)
```

所有 Block 继续使用 `type` 判别字段：

```text
text
thinking
data
hint
tool_call
tool_result
```

核心约束：

- 数组顺序就是消息顺序；
- `Msg.id` 在 FTRE 配置目录内应全局唯一；
- User/System/Assistant 角色约束继续由 `Msg` 模型验证；
- 不额外保存 Event 类型、Event data 或 `reply_id` 列；
- AssistantMsg 的 `id` 本身可等于 reply ID；
- `timestamp` 不写入磁盘，API 游标值由 `Msg.created_at` 转为 epoch；
- `usage`、`finished_reason`、`error` 等属于 Msg，保留；
- `TEXT_BLOCK_DELTA` 等流式 Event 名称不应出现在状态结构中。

### 6.4 `summary`

无 compact 时：

```json
{
  "summary": null
}
```

compact 后：

```json
{
  "summary": {
    "message": {
      "id": "summary_002",
      "name": "context_compact",
      "role": "system",
      "content": [
        {
          "type": "text",
          "id": "summary_block_002",
          "text": "截至 reply_100 的滚动上下文摘要。",
          "created_at": "2026-07-27T21:00:00+08:00",
          "finished_at": "2026-07-27T21:00:00+08:00"
        }
      ],
      "metadata": {
        "context_compact": {
          "mode": "summary",
          "trigger": "idle",
          "tokens_before": 70000,
          "tokens_after": 6000
        }
      },
      "created_at": "2026-07-27T21:00:00+08:00",
      "usage": null,
      "finished_at": "2026-07-27T21:00:00+08:00",
      "finished_reason": "completed",
      "structured_output": null,
      "error": null
    },
    "through_message_id": "reply_100"
  }
}
```

约束：

- `summary.message` 必须是完整 SystemMsg；
- `summary.message` 不放入 `messages`；
- `through_message_id` 必须引用 `messages` 中的一条真实消息；
- 摘要覆盖从 Session 开始到 `through_message_id`；
- `summary` 只保存当前有效的滚动摘要；
- 不保存所有历史摘要版本。

### 6.5 多次 compact

第一次：

```text
summary1 = summarize(messages[0..A])
through_message_id = A
```

第二次：

```text
summary2 = summarize(summary1 + messages[A+1..B])
through_message_id = B
```

状态文件中直接用 `summary2` 覆盖 `summary1`，原始 `messages` 不删除。

给模型构建上下文时：

```text
summary.message
+ messages 中 through_message_id 后面的所有 Msg
```

Desktop 获取历史时：

```text
messages 全量
```

因此 compact 只改变模型上下文视图，不改变用户可见历史。

### 6.6 `metadata`

Session 扩展数据统一存入顶层 `metadata`：

```json
{
  "metadata": {
    "plan": {},
    "external": {
      "channel_id": "octo",
      "external_key": "octo:2:ch_group_1",
      "data": {
        "channel_type": 2,
        "channel_id": "ch_group_1",
        "from_uid": "uid_alice"
      },
      "created_at": "2026-07-27T20:00:00+08:00",
      "updated_at": "2026-07-27T20:10:00+08:00"
    }
  }
}
```

第一版继续允许任意 JSON 值，但核心保留键应使用稳定名称：

| 键 | 用途 |
|---|---|
| `plan` | Plan 工具和插件状态 |
| `external` | 外部平台会话映射 |

新插件应优先使用带命名空间的键，例如 `octo.settings`，避免键名冲突。

## 7. 推荐 Pydantic 模型

新建 `src/ftre/session/state.py`：

```python
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ftre_agent_core.message import Msg


class SessionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    agent_id: str = "default"
    channel_id: str
    title: str = ""
    workspace: str = ""
    created_at: str
    updated_at: str


class SummaryState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: Msg
    through_message_id: str

    @model_validator(mode="after")
    def validate_summary_message(self) -> "SummaryState":
        if self.message.role != "system":
            raise ValueError("summary.message must be a SystemMsg")
        return self


class AgentStateFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session: SessionState
    messages: list[Msg] = Field(default_factory=list)
    summary: SummaryState | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references(self) -> "AgentStateFile":
        ids = [message.id for message in self.messages]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate Msg.id in session")
        if (
            self.summary is not None
            and self.summary.through_message_id not in set(ids)
        ):
            raise ValueError("summary cursor does not reference a message")
        return self
```

JSON Schema 应从 Pydantic 生成，不手工维护第二份 Msg/Block Schema：

```python
schema = AgentStateFile.model_json_schema()
```

如需将 Schema 纳入仓库，可生成：

```text
docs/schemas/agent-state-v1.schema.json
```

## 8. SessionManager 对外契约

现有调用方不应直接读写文件。`SessionManager` 继续作为唯一入口。

### 8.1 保持不变的接口

```python
await manager.init()
await manager.close()

await manager.create_session(...)
await manager.get_session(...)
await manager.update_session(...)
await manager.delete_session(...)
await manager.list_sessions(...)
await manager.count_sessions(...)
await manager.list_workspaces(...)

await manager.save_message(...)
await manager.update_message(...)
await manager.get_messages_by_session(...)
await manager.get_recent_messages_by_turns(...)
await manager.get_token_usage(...)

await manager.get_session_metadata(...)
await manager.update_session_metadata(...)

await manager.get_or_create_external_session(...)
await manager.get_external_session(...)
```

### 8.2 新增接口

```python
async def get_context_messages(
    self,
    session_id: str,
) -> list[MessageModel]:
    """返回给 LLM 使用的 summary + tail。"""


async def get_summary(
    self,
    session_id: str,
) -> SummaryState | None:
    """返回当前滚动摘要。"""


async def save_summary(
    self,
    session_id: str,
    message: Msg,
    *,
    through_message_id: str,
) -> None:
    """原子更新当前摘要，不把摘要加入 transcript。"""
```

### 8.3 两种消息读取语义

必须禁止一个方法同时服务 Desktop 历史和 LLM 上下文。

| 方法 | 语义 | 调用方 |
|---|---|---|
| `get_messages_by_session` | 完整 transcript | HTTP API、Desktop、历史展示 |
| `get_context_messages` | 当前 summary + 未覆盖 tail | Agent 构建、token 统计 |

`get_context_messages` 逻辑：

```python
if state.summary is None:
    return all_messages

cursor = find_index(state.summary.through_message_id)
return [
    message_to_api_record(state.summary.message, session_id),
    *all_messages[cursor + 1:],
]
```

摘要 SystemMsg 保留 `metadata.context_compact.mode == "summary"`，现有 converter 可继续识别。后续可进一步简化 converter，但不属于第一阶段必要改动。

## 9. 内存索引与并发模型

### 9.1 进程内缓存

`SessionManager.init()` 扫描 `~/.ftre/sessions/*.json` 并建立：

```python
self._states: dict[str, AgentStateFile]
self._message_sessions: dict[str, str]
self._external_sessions: dict[tuple[str, str], str]
self._locks: dict[str, asyncio.Lock]
self._global_lock: asyncio.Lock
```

第一版直接把所有 AgentState 加载到内存。FTRE 当前是个人本地 Gateway，Session 数量和状态文件规模可接受。

未来当启动时间或内存占用成为问题时，再引入可重建的 `index.json` 和懒加载；第一版不提前增加这一复杂度。

### 9.2 锁规则

- 一个 Session 一个 `asyncio.Lock`；
- 创建 Session、删除 Session和外部映射创建使用 `_global_lock`；
- 所有读改写操作必须在 Manager 内部持锁；
- 调用方不能拿到可直接修改的内部 `AgentStateFile` 引用；
- 返回 API dict 或模型副本，避免锁外修改缓存。

### 9.3 避免丢更新

错误做法：

```text
compact 读取整个 state
用户消息写入新 state
compact 用旧副本覆盖整个文件
→ 用户消息丢失
```

正确做法：

```text
compact 在锁外完成 LLM 摘要
save_summary() 获取 Session 锁
读取当前缓存中的最新 state
只更新 summary 字段
写入完整最新 state
```

即使 compact 期间新增了消息，摘要的 `through_message_id` 仍只覆盖 compact 开始时捕获的最后一条消息，新增消息自然留在 tail。

## 10. 原子文件写入

每次状态变更都立即写盘，不使用延迟缓存：

```python
async def _write_state(state: AgentStateFile) -> None:
    payload = state.model_dump_json(indent=2)
    await asyncio.to_thread(_atomic_replace, path, payload)
```

同步原子替换过程：

```python
def _atomic_replace(path: Path, payload: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
```

要求：

- 临时文件必须与目标文件位于同一目录；
- 替换前 payload 已通过 Pydantic 校验；
- 写盘失败时内存缓存不能提前提交为新状态；
- 成功替换后再更新内存缓存，或者使用不可变副本计算并一次提交；
- 启动时发现残留 `.tmp` 不自动覆盖正式文件；
- 正式文件损坏时不得静默创建空 Session；
- 损坏文件应记录错误并隔离为 `.corrupt-<timestamp>`，同时让该 Session 明确加载失败。

不建议每次自动保留 `.bak`，否则长时间运行会再次产生大量过期备份。

## 11. 消息写入规则

### 11.1 UserMsg

用户消息被接受后立即保存：

```text
Inbound
→ 构造完整 UserMsg
→ save_message()
→ 原子写 state.json
→ 开始 Agent
```

这保证即使模型调用失败，用户输入仍然存在。

### 11.2 AssistantMsg

正常情况：

```text
AgentStreamEvent
→ 内存 Msg.append_event()
→ REPLY_END
→ 完整 AssistantMsg
→ save_message()
```

异常和取消：

```text
error / cancel
→ 将当前内存聚合 Msg 标记 finished_reason
→ 保存一次部分 AssistantMsg
```

严禁：

```text
每个 TEXT_BLOCK_DELTA 重写 state.json
```

### 11.3 `update_message`

第一版继续支持 compact-fast 修改已保存的 ToolResultBlock。

通过 `_message_sessions[msg.id]` 找到所属 Session，获取对应锁后更新。

如果消息 ID 不存在，应抛出明确异常或返回 `False`，不能静默成功。

说明：第一版不实现 ToolResult 原文与活动上下文 stub 的双层 overlay，因此 compact-fast 仍会修改持久化 Msg；该限制应在后续独立改造中解决。

## 12. compact 改造

当前 compact 将摘要作为 SystemMsg 追加进消息历史，并依赖 converter 遇到摘要标记后清空之前的消息。

新设计改为：

```text
完整历史       state.messages
当前摘要       state.summary.message
摘要游标       state.summary.through_message_id
```

### 12.1 should_compact

`get_token_usage(session_id)` 应对 `get_context_messages()` 计算，而不是对完整 transcript 计算。

否则 compact 后原始消息仍在 `messages`，token 统计会一直认为上下文没有缩小。

### 12.2 执行 compact

流程：

1. `context_messages = get_context_messages(session_id)`；
2. 取当前 `summary`；
3. 找到 summary 后的 tail；
4. 确定本次最后覆盖的真实 Msg ID；
5. 调 LLM 生成滚动摘要；
6. 构造完整 SystemMsg；
7. 调用 `save_summary(..., through_message_id=last_real_msg_id)`；
8. 不调用 `save_message(summary_message)`。

### 12.3 失败语义

- LLM 失败：保持原 summary 和 messages 不变；
- 新摘要膨胀：保持旧 summary 不变；
- `through_message_id` 在保存时不存在：拒绝保存；
- compact 期间新增消息：新增消息保留在摘要游标之后；
- idle compact 与前台 turn 并发：最终由 Session 锁保证字段级提交不覆盖消息。

## 13. 分页和时间游标

磁盘不再保存单独的 `timestamp`。

对外 `MessageModel["timestamp"]` 由：

```python
datetime.fromisoformat(msg.created_at).timestamp()
```

派生。

`get_recent_messages_by_turns()` 改为内存过滤：

1. 取完整 `messages`；
2. 如果有 `before_ts`，过滤 `created_at_epoch < before_ts`；
3. 从后向前找到最近 N 条可见 UserMsg；
4. 返回最早目标 UserMsg 到末尾的所有消息；
5. 根据前面是否还有消息计算 `has_more`。

可见 UserMsg 规则保持：

```python
msg.role == "user" and not msg.metadata.get("hide", False)
```

如果未来允许完全相同的 `created_at`，分页游标应升级为：

```text
(created_at, message_id)
```

第一版保持现有 float API，避免 Desktop 联动扩大。

## 14. 外部 Session 映射

原 `external_sessions` 表迁移到：

```text
state.metadata.external
```

启动时建立：

```python
_external_sessions[(channel_id, external_key)] = session_id
```

`get_or_create_external_session()`：

1. 获取全局锁；
2. 查询内存外部映射；
3. 已存在：更新对应 Session 的 `metadata.external.data` 和 `updated_at`；
4. 不存在：创建 Session，写入 external metadata，更新映射；
5. 原子写入 Session 文件。

第一版保持“一份本地 Session 最多一个 external binding”的现有实际语义。

## 15. 旧 SQLite 迁移

### 15.1 原则

- 迁移只读旧 SQLite；
- 不删除、不重命名、不覆盖 `sessions.db`；
- 每个 Session 独立迁移；
- 已存在的目标 JSON 不覆盖；
- 整体可重复执行；
- 所有 Session 成功后才写迁移完成标记；
- 失败时下次启动继续；
- 迁移完成后运行时不再向 SQLite 写入。

### 15.2 迁移入口

新建：

```text
src/ftre/session/migrate_sqlite.py
```

`SessionManager.init()`：

```python
if legacy_db.exists() and not migration_marker.exists():
    await migrate_sqlite_to_json(...)
await load_json_states()
```

SQLite 读取使用 Python 标准库 `sqlite3`，放入 `asyncio.to_thread()`，不再依赖 `aiosqlite`。

### 15.3 Session 字段

```text
sessions.id          → session.id
sessions.channel_id  → session.channel_id
sessions.title       → session.title
sessions.workspace   → session.workspace
sessions.created_at  → ISO session.created_at
sessions.updated_at  → ISO session.updated_at
sessions.metadata    → metadata
```

`agent_id` 迁移策略：

1. 优先取最新 UserMsg 的 `metadata.agent_id`；
2. 其次取最新 UserMsg 的 `name`；
3. 默认 `"default"`。

### 15.4 Messages

按旧 `messages.timestamp ASC` 读取。

每行先恢复完整 Msg：

```python
Msg.model_validate({
    "id": row["id"],
    "name": row["name"],
    "role": row["role"],
    "content": json.loads(row["content"]),
    "metadata": json.loads(row["metadata"]),
    "created_at": row["created_at"],
    "usage": load_optional_json(row["usage"]),
    "finished_at": row["finished_at"],
    "finished_reason": row["finished_reason"],
    "structured_output": load_optional_json(row["structured_output"]),
    "error": load_optional_json(row["error"]),
})
```

无法验证的行必须记录 Session ID、Msg ID 和错误，不得静默丢弃后继续标记迁移成功。

### 15.5 迁移已有 compact 摘要

旧库可能包含多条：

```text
role=system
name=context_compact
metadata.context_compact.mode=summary
```

迁移规则：

1. 找到最后一条 summary compact；
2. 找到它之前最近的一条非 summary Msg；
3. 将最后一条摘要转为 `state.summary.message`；
4. 将前一条真实 Msg ID 设为 `through_message_id`；
5. 所有 summary compact Msg 从 `state.messages` 排除；
6. 其他 User/Assistant/System Msg 保持原顺序；
7. 如果摘要之前没有真实消息，记录迁移错误，不构造无效游标。

旧摘要历史不继续保存，因为 v1 只保留当前有效滚动摘要。

### 15.6 外部映射

```text
external_sessions.channel_id    → metadata.external.channel_id
external_sessions.external_key  → metadata.external.external_key
external_sessions.external_data → metadata.external.data
created_at / updated_at          → ISO 字符串
```

### 15.7 迁移验证

每个 Session 写入前验证：

- Session ID 相同；
- Msg 数量等于旧库非摘要 Msg 数量；
- Msg ID 集合相同；
- 角色计数相同；
- 所有 Msg 通过 Pydantic；
- summary 游标存在；
- 不出现 Event 行；
- external mapping 唯一。

所有 Session 成功后写：

```text
~/.ftre/sessions/.sqlite-migrated-v1
```

标记内容建议包含：

```json
{
  "schema_version": 1,
  "source": "C:\\Users\\<user>\\.ftre\\sessions.db",
  "migrated_at": "2026-07-27T22:00:00+08:00",
  "sessions": 12,
  "messages": 340
}
```

## 16. 文件改造清单

### 16.1 新增

| 文件 | 职责 |
|---|---|
| `src/ftre/session/state.py` | Pydantic `SessionState`、`SummaryState`、`AgentStateFile` |
| `src/ftre/session/json_store.py` | 路径编码、扫描、原子读写、锁和内存索引 |
| `src/ftre/session/migrate_sqlite.py` | 旧 SQLite 只读迁移 |
| `tests/test_session_state.py` | Schema、引用和 Msg 验证 |
| `tests/test_session_json_store.py` | CRUD、原子写、并发、分页 |
| `tests/test_session_sqlite_migration.py` | 迁移与幂等 |

### 16.2 修改

| 文件 | 修改 |
|---|---|
| `src/ftre/session/manager.py` | 保留公开 API，底层改用 JSON Store |
| `src/ftre/session/converter.py` | 必要时简化 summary 输入，但第一阶段可保持兼容 |
| `src/ftre/agent/turn_executor.py` | Agent 构建改用 `get_context_messages()` |
| `src/ftre/agent/compact_manager.py` | 摘要改用 `get_summary()` / `save_summary()` |
| `src/ftre/main.py` | 更新 SQLite 注释和启动日志 |
| `tests/test_event_stream_history.py` | 从 SQL 列断言改为 AgentState JSON 断言 |
| `tests/test_external_sessions.py` | 测试目标路径改为 JSON 状态目录 |
| `pyproject.toml` | 迁移完成且无其他用途后移除 `aiosqlite` |

### 16.3 不需要修改

HTTP Session API 响应保持兼容时，Desktop 原则上无需修改。但必须运行 Desktop 的历史加载测试与生产构建验证。

## 17. 分阶段执行计划

### Phase 1：锁定现有行为

- [ ] 补齐 SessionManager 现有接口测试；
- [ ] 固定 Session API 响应结构；
- [ ] 固定最近 N 轮分页语义；
- [ ] 固定 external mapping 更新语义；
- [ ] 固定 compact 前后 provider messages；
- [ ] 确认现有用户改动 `tests/test_octo_channel.py` 不纳入本次修改。

验收：

```text
现有测试全部通过，新增测试先针对 SQLite 实现建立行为基线。
```

### Phase 2：实现 Schema

- [ ] 新建 `state.py`；
- [ ] 实现五字段根模型；
- [ ] 校验 Msg ID 唯一；
- [ ] 校验 summary 是 SystemMsg；
- [ ] 校验 summary 游标引用真实消息；
- [ ] 生成并检查 JSON Schema；
- [ ] 覆盖未知 `schema_version` 测试。

验收：

```text
合法 AgentState 可 round-trip；
非法 Msg、重复 ID、悬空 summary cursor 被拒绝。
```

### Phase 3：实现 JSON Store

- [ ] 实现 Session ID 的 base64url 目录名；
- [ ] 实现安全路径解析；
- [ ] 实现 JSON 扫描和 Pydantic 加载；
- [ ] 实现 per-session lock；
- [ ] 实现全局 create/delete lock；
- [ ] 实现临时文件 + fsync + os.replace；
- [ ] 实现消息和外部映射内存索引；
- [ ] 实现损坏文件显式报错。

验收：

```text
并发保存两条消息不丢失；
模拟写入失败后旧 state.json 保持完整；
进程重启后状态可恢复。
```

### Phase 4：替换 SessionManager

- [ ] 将 Session CRUD 切到 JSON Store；
- [ ] 将 metadata CRUD 切到 JSON Store；
- [ ] 将 external mapping 切到 metadata.external；
- [ ] 将 Msg 保存和更新切到 `messages`；
- [ ] 在 API 出口恢复 `MessageModel`；
- [ ] 实现内存分页；
- [ ] 让 `close()` 成为安全、幂等操作。

验收：

```text
API 行为测试不变；
磁盘不创建新的 SQLite 行；
state.json 可直接阅读。
```

### Phase 5：分离 transcript 和 model context

- [ ] 新增 `get_context_messages()`；
- [ ] `get_messages_by_session()` 保持完整历史；
- [ ] Agent 构建改用 context messages；
- [ ] token usage 改用 context messages；
- [ ] 增加 summary + tail 转换测试。

验收：

```text
Desktop 返回完整历史；
LLM 只收到 summary + tail；
compact 后 token 统计明显下降。
```

### Phase 6：改造 CompactManager

- [ ] 读取当前 summary；
- [ ] 生成滚动摘要；
- [ ] 记录本次 `through_message_id`；
- [ ] 使用 `save_summary()`；
- [ ] 禁止将摘要追加进 transcript；
- [ ] 验证 compact 并发新增消息不丢失；
- [ ] 保持 compact start/done/failed WebSocket 通知。

验收：

```text
连续执行两次 compact 后只有一个 active summary；
所有原始 Msg 仍存在；
第二次摘要覆盖范围正确；
新增 tail 未被错误覆盖。
```

### Phase 7：实现 SQLite 迁移

- [ ] 使用标准库 sqlite3 只读旧库；
- [ ] 迁移 Session；
- [ ] 迁移 Msg；
- [ ] 抽取最后一个 summary；
- [ ] 迁移 external mapping；
- [ ] 按 Session 原子写 JSON；
- [ ] 实现幂等跳过；
- [ ] 全量成功后写 marker；
- [ ] 失败时保留旧库和错误上下文。

验收：

```text
旧库和 JSON 的 Session/Msg 计数一致；
全部 Msg 可验证；
重复启动不会重复消息；
旧 sessions.db 未被修改。
```

### Phase 8：联动验证

- [ ] 后端 Session、compact、turn 生命周期测试；
- [ ] 完整后端测试；
- [ ] Core 测试，确认 Msg Schema 未漂移；
- [ ] Desktop 历史加载测试；
- [ ] Desktop production build；
- [ ] 停止旧 Gateway；
- [ ] 启动新 Gateway；
- [ ] 创建真实测试 Session；
- [ ] 执行一轮含 ToolCall 的对话；
- [ ] 执行 compact；
- [ ] 重启 Gateway；
- [ ] 验证 Session、完整历史和 summary 恢复；
- [ ] 删除测试 Session；
- [ ] 保留用户真实 Session。

## 18. 测试矩阵

### 18.1 Schema

| 用例 | 预期 |
|---|---|
| 最小合法状态 | 通过 |
| 未知根字段 | 拒绝 |
| `schema_version=2` | 明确报不支持 |
| Msg ID 重复 | 拒绝 |
| summary 不是 system | 拒绝 |
| summary cursor 不存在 | 拒绝 |
| Event 形状混入 messages | Msg 校验失败 |

### 18.2 文件安全

| 用例 | 预期 |
|---|---|
| Session ID 包含 `::` | 生成合法文件名 |
| Unicode Session ID | round-trip |
| 写入中途异常 | 正式文件不变 |
| `.tmp` 残留 | 不覆盖正式文件 |
| JSON 损坏 | 明确报错，不返回空 Session |
| 删除 Session | 只删除精确目标文件 |

### 18.3 并发

| 用例 | 预期 |
|---|---|
| 两个并发 `save_message` | 两条都存在 |
| `save_message` + `update_metadata` | 两项修改都存在 |
| idle compact + 新 UserMsg | 新消息保留在 summary tail |
| `update_message` + Session 删除 | 明确成功一种顺序，不能损坏其他文件 |

### 18.4 compact

| 用例 | 预期 |
|---|---|
| 第一次 compact | summary 创建，原消息保留 |
| 第二次 compact | summary 替换，cursor 前进 |
| LLM 失败 | 状态不变 |
| 摘要膨胀 | 状态不变 |
| Desktop 获取历史 | 不返回 synthetic summary |
| Agent 构建上下文 | 返回 summary + tail |

### 18.5 迁移

| 用例 | 预期 |
|---|---|
| 空旧库 | 成功，0 Session |
| 普通消息 | Msg 数量、ID、角色一致 |
| 多次 compact | 只迁移最后摘要，cursor 正确 |
| external mapping | 可被复用和更新 |
| 非法 Msg 行 | 迁移失败且无 marker |
| 重复迁移 | 不重复写消息 |
| 目标 JSON 已存在 | 不覆盖 |

## 19. 启动、日志与故障处理

建议启动日志：

```text
[session-store] backend=json directory=C:\Users\<user>\.ftre\sessions
[session-store] loaded sessions=32 messages=418
[session-migration] source=sessions.db migrated=32 skipped=0 failed=0
```

单文件损坏：

```text
[session-store] invalid state file path=... session_hint=... error=...
```

禁止：

- 自动当作空 Session；
- 自动覆盖为新文件；
- 吞掉 Pydantic 错误；
- 因一份文件损坏而删除整个 sessions 目录。

是否允许其他 Session 继续加载，可由实现选择；推荐继续启动，但 API 访问损坏 Session 时返回明确错误。

## 20. 回滚策略

迁移期间旧 `sessions.db` 始终保持不变，因此回滚只需要：

1. 停止 Gateway；
2. 切回旧代码；
3. 让旧代码重新读取 `sessions.db`。

注意：切到 JSON Store 之后新产生的 Session 和消息不会自动回写旧 SQLite。若需要在上线后回滚并保留新数据，应额外实现：

```text
JSON → SQLite 回迁脚本
```

因此推荐发布步骤：

1. 先在测试配置目录验证；
2. 再在真实配置目录迁移；
3. 保留旧库；
4. 观察至少一个完整使用周期；
5. 确认无需回滚后，再由用户明确决定是否删除旧库。

任何自动删除旧数据库的行为都不属于本计划。

## 21. 验收标准

改造完成必须同时满足：

- `~/.ftre/sessions/` 下每个 Session 一份合法 `state.json`；
- 根结构只有 `schema_version/session/messages/summary/metadata`；
- 所有 `messages[]` 均可验证为 `Msg`；
- 状态文件中不存在流式 Event 记录；
- Session API 与 Desktop 保持兼容；
- Desktop 能查看完整历史；
- LLM 只收到 summary + tail；
- 多次 compact 不删除原始消息；
- Gateway 重启后 Session、消息、摘要、metadata、external mapping 均恢复；
- 并发写入不丢消息；
- 原子写失败不损坏旧文件；
- 旧 SQLite 迁移可重复、可验证；
- 旧 `sessions.db` 未被自动删除；
- 后端、Core、Desktop 相关测试全部通过。

## 22. 推荐实施顺序总结

不要从“删除 SQLite 代码”开始。正确顺序是：

```text
行为基线测试
→ Pydantic Schema
→ JSON Store
→ SessionManager 切换
→ transcript/context 分离
→ compact 改造
→ SQLite 迁移
→ Gateway 与 Desktop 联动验证
→ 最后才移除 aiosqlite
```

最核心的不变量：

```text
Msg 是持久化事实
summary 是上下文视图
Event 是运行过程
```
