# ftre Services：运行时能力说明

`services/` 是 ftre 的“公共能力层”。这里的对象会被多个 Plugin、HTTP 路由、
Channel 或 Agent 数据面共同使用，因此每个 Service 都有稳定的 `key`，并由
Composition Root 通过 Provider Plugin 创建和销毁。

## 先记住三条边界

1. **Service 保存状态并提供窄接口。** 例如 `SessionService` 负责会话持久化，
   `ToolService` 负责工具注册和视图，调用方不应该绕过 Service 直接改 JSON、全局
   注册表或 AgentLoop 的私有字段。
2. **Provider Plugin 只负责装配。** `plugin.py` 中的 `inject` 表示依赖的 Service
   key，`provide` 表示发布的 Service key；资源、监听器、路由和后台任务都要通过
   `ctx.effect(...)` 绑定到当前 Fiber，卸载时才能逆序清理。
3. **Plugin 不拥有别人的基础设施。** Plugin 可以消费公开 Service 或 Hook，但不能
   import 另一个 Service 的私有 Provider、存储实现或 Agent Runtime 内部对象。

## Service 清单

| Service key | 实现位置 | 负责什么 | 主要依赖/消费者 |
| --- | --- | --- | --- |
| `config` | `config/service.py` | 配置内存快照、revision、原子写入和 watcher | Composition、各 Plugin |
| `filesystem` | `filesystem/local.py` | 统一路径解析、工作区边界和文件读写 | Workspace、Tools、Features |
| `http` | `http/service.py` | 收集路由贡献、冲突检查、冻结后构建 FastAPI | Gateway Host、各路由 Plugin |
| `message_bus` | `messaging/bus/service.py` | Inbound/Outbound 的进程内消息队列门面 | Channel、AgentLoop |
| `channels` | `messaging/channel/service.py` | Channel 注册、启动/停止和发送 | WebSocket、Subagent、Cron |
| `sessions` | `session/service.py` | Session 身份、Msg 历史和 Session 元数据的唯一持久化入口 | AgentLoop、Workspace、Command |
| `session_events` | `session/events.py` | 将可选 Feature 事件接入 SessionProjection | AgentLoop Provider、Feature |
| `agents` | `agent/service.py` | Agent 身份、状态和公开数据面 Driver | HTTP/WS、AgentLoop Provider |
| `agent_profiles` | `agent/profile/service.py` | Agent 配置文件的 CRUD 与解析 | Agent、MCP、Tools |
| `tools` | `tools/service.py` | 全局工具、Agent scoped 工具和 allow/deny 视图 | AgentLoop、Tool Feature |
| `workspaces` | `workspace/service.py` | Session 工作区选择和 `PathPolicy` 构造 | Tools、Workspace API |
| `attachments` | `attachment/service.py` | 请求附件的安全保存、读取和 MIME 判断 | WebSocket、HTTP |
| `system_prompt` | `system_prompt/service.py` | 有序、按 scope 的 Prompt section 组装 | AgentLoop、Prompt Feature |
| `traces` | `plugins/builtin/trace/service.py` | Agent trace 的 SQLite 查询门面 | Trace Plugin、诊断工具 |

`services/agent/runtime/` 是 AgentService 的私有执行实现；它不是独立 Service。唯一公开
的 Agent key 是 `agents`，HTTP/WS 和 Plugin 只依赖这个 Service，不会拿到 Loop/Executor。

## 一条消息如何经过这些 Service

```text
Channel
  → MessageBusService.request_inbound()
  → AgentService（内部 Runtime）
  → ftre-inbox.InboxService
  → SystemPromptService + ToolService
  → ftre-agent-core / LLM
  → SessionEventService + SessionService 持久化投影
  → MessageBusService.publish_outbound()
  → ChannelService / Channel
```

- `MessageBusService` 只负责传递，不保存会话业务状态。
- `SessionService` 负责 Session/Msg 的持久化历史；独立 `InboxService` 负责 durable
  pending、串行交付和恢复；AgentService 只执行已交付输入。
- `ToolService` 和 `SystemPromptService` 提供本轮可见的工具/提示词视图，不能把
  全局注册表直接暴露给 Agent。
- `ChannelService` 只管理协议通道，不参与模型推理或 Session 业务规则。

## 生命周期口诀

```text
provide → start → use → stop → dispose
```

Provider 的 `apply()` 不应偷偷启动另一个全局进程，也不应在模块 import 时创建
单例。所有副作用都要属于当前 Cordis Fiber；没有可逆 disposer 的资源就不能算作
完成了 Service 接入。
