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
- **非目标**：不实现 SessionLane 并发控制（B1）、不实现上下文压缩（B2）。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：SessionManager 创建/删除/列表——支持创建新 session、按 ID 查询、列出全部、删除指定 session
- [x] FR2：Repository JSON 持久化——Session 状态和消息历史以 JSON 文件持久化到 `~/.ftre/sessions/` 目录
- [x] FR3：AgentStateFile / SessionState——定义会话实体和状态模型，包含 session_id、创建时间、工作区等元信息
- [x] FR4：Msg 持久化——每条消息（UserMessage / AssistantMessage）持久化为 JSON，含 role、content、timestamp
- [x] FR5：fork 分叉——从现有 session 创建分叉，继承 messages 历史但不继承 mailbox pending 队列

### 2.2 非功能需求

- 性能：单个 session 消息历史加载 < 100ms
- 安全：session 文件按用户隔离，路径不越界
- 兼容性：JSON 格式向前兼容，新增字段时旧数据可读

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
@dataclass
class SessionState:
    session_id: str
    created_at: datetime
    workspace: str
    agent_id: str

@dataclass
class Msg:
    role: str          # "user" | "assistant" | "system"
    content: list      # content blocks
    timestamp: str
```

## 5. 验收标准

- [x] AC1：session CRUD 全部正确——创建后可按 ID 查询，列表包含已创建 session，删除后不可查
- [x] AC2：JSON 持久化可恢复——写入消息后重启 Gateway，消息历史完整加载
- [x] AC3：fork 继承 messages 不继承 mailbox——fork 后新 session 拥有原 session 的全部消息历史，但 pending 队列为空
