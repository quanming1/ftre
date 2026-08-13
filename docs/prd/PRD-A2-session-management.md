# PRD-A2-Session管理

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | A2 |
| 名称 | Session 管理（SessionManager + Repository + entity/state + JSON 持久化 + fork） |
| 状态 | 已验收 |
| 创建日期 | 2026-08-12 |
| 定稿日期 | 2026-08-12 |
| 验收日期 | 2026-08-12 |
| 关联文档 | docs/TODO.yaml 阶段 A2；AGENTS.md |

## 1. 背景与目标

- **背景**：Gateway 需要持久化会话状态和消息历史。用户可能同时有多个会话，每个会话有独立的消息流和 agent 状态。需要可靠的 JSON 持久化方案，确保重启后可恢复。
- **目标**：实现完整的 Session 生命周期管理——创建、查询、删除、fork 分叉，消息历史 JSON 持久化，重启后可恢复。
- **非目标**：不实现 SessionLane 并发控制（B1）、不实现上下文压缩（B2）。A2 只定义 Session 数据和落盘边界，运行编排由 B1 负责。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：SessionManager 创建/删除/列表——支持创建新 session、按 ID 查询、列出全部、删除指定 session
- [x] FR2：Repository JSON 持久化——Session 状态和消息历史以 JSON 文件持久化到 `~/.ftre/sessions/` 目录
- [x] FR3：AgentStateFile / SessionState——定义会话实体和状态模型，包含 session_id、创建时间、工作区等元信息
- [x] FR4：Msg 持久化——每条消息（UserMessage / AssistantMessage）持久化为 JSON，含 role、content、timestamp
- [x] FR5：fork 分叉——从现有 session 创建分叉，继承 messages 历史但不继承 mailbox pending 队列
- [x] FR6：严格状态文件——`state.json` 按固定 schema 校验；损坏或不支持的版本被隔离并明确报错，不静默创建空 Session
- [x] FR7：原子持久化——写入使用同目录临时文件、fsync、原子替换；只有写盘成功后才更新内存索引

### 2.2 非功能需求

- 性能：单个 session 消息历史加载 < 100ms
- 安全：session 文件按用户隔离，路径不越界
- 兼容性：当前 schema 版本严格匹配；不提供旧格式迁移，历史数据清理后按当前 schema 重新创建

## 3. 技术方案

### 模块设计

| 文件 | 职责 |
|---|---|
| `src/ftre/session/manager.py` | `SessionManager`，session CRUD + fork |
| `src/ftre/session/storage/repository.py` | `Repository`，JSON 读写抽象 |
| `src/ftre/session/entity/state.py` | `SessionState`、`AgentStateFile` 实体定义 |
| `src/ftre/session/entity/models.py` | `Msg`、`UserMessage`、`AssistantMessage` 数据模型 |
| `src/ftre/session/storage/json_store.py` | `JsonStore`，文件系统 JSON 持久化实现 |

### 关键数据结构

```python
class AgentStateFile(BaseModel):
    schema_version: Literal[1] = 1
    session: SessionState
    messages: list[Msg]
    mailbox: MailboxState
    metadata: dict[str, Any]

class MailboxState(BaseModel):
    revision: int
    next_sequence: int
    pending: list[QueueItem]
```

## 4. 持久化边界与恢复语义

`state.json` 是 Session 的唯一持久事实源，但不同数据的可靠性等级不同：

| 数据 | 是否写入 `state.json` | 重启后的行为 |
|---|---:|---|
| Session 元信息 | 是 | 正常恢复 |
| `messages` 中已投影的 User/Assistant/compact Msg | 是 | 从历史恢复；已写入的 UserMsg 作为 request_id 幂等凭据 |
| `mailbox.pending` | 是 | Gateway 启动时恢复并由 SessionLane 重新消费 |
| 当前 active Turn | 否 | 不自动重放，避免工具副作用重复；已 checkpoint 的 Reply Msg 保留 |
| CompletionRegistry 结果 | 否 | 只服务当前进程内的 task/team 等同步等待 |

关键规则：

- `admit_request` 的容量检查、request_id 去重、sequence 分配和 state.json 提交必须在同一 Session 锁内完成。
- `take_pending_request` 是 at-most-once 交接点：从 pending 移除后，消息可能在写入 UserMsg 前因进程退出而丢失，这是明确接受的异常语义。
- Schema 解析失败的文件改名为 `state.json.corrupt-*` 并保留取证；不能把损坏文件当作空 Session。
- fork 只复制 messages 和允许继承的 metadata；新 Session 的 mailbox 必须为空，不能把父 Session 的待执行消息复制过去。

### 4.1 接口定义

- `SessionManager.create_session / get_session / delete_session / fork_session`：Session 生命周期。
- `SessionManager.admit_inbound / peek_request / take_pending_request / cancel_pending_request`：仅供 B1 的 MailboxStore 使用。
- `GET /api/sessions/{session_id}/messages`：返回历史 messages、当前公开 status 和 mailbox 快照。
- `GET /api/sessions/{session_id}/state`：分页读取原始 state 投影；不改变 Session 运行状态。
- `DELETE /api/sessions/{session_id}/queue/{request_id}`：只取消 pending；已领取消息返回冲突，不替代 active Turn cancel。

## 5. 验收标准

- [x] AC1：session CRUD 全部正确——创建后可按 ID 查询，列表包含已创建 session，删除后不可查
- [x] AC2：JSON 持久化可恢复——写入消息后重启 Gateway，消息历史完整加载
- [x] AC3：fork 继承 messages 不继承 mailbox——fork 后新 session 拥有原 session 的全部消息历史，但 pending 队列为空

## 6. 测试计划

- `tests/test_session_json_store.py`：原子写入、损坏文件隔离、路径安全。
- `tests/test_session_state.py`：schema_version、额外字段、Msg/mailbox 校验。
- `tests/test_session_fork.py`：messages 复制、mailbox 不继承、fork 溯源 metadata。
- `tests/test_session_manager_concurrency.py`：同 Session 并发写入的锁和索引一致性。
- 手动验收：删除 pending 后刷新 API/WS 快照；重启后仅恢复 pending，不重放 active。

## 7. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-08-13 | 补充 AgentStateFile/MailboxState 真实模型、原子落盘、损坏隔离、active/completion 非持久化和 HTTP 接口；修正“向前兼容”表述 | 使 A2 与当前 Pydantic schema、Mailbox 恢复策略和用户确认的历史清理策略一致 |
| 2026-08-13 | 影响复核：A2 的持久化语义被细化；AC1-AC3 仍需以现有 Session 测试和手动重启验收为依据，本次未改代码 | 防止文档澄清被误认为实现变更 |
