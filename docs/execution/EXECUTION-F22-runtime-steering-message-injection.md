# F22 执行报告：运行中 Steering 消息注入与协议闭环

## 结果

F22 已完成。普通消息先进入 `queue`，客户端可以把同一条 pending item 升级为
`steer`；Inbox 在 Core 的 `agent/before-reasoning` Hook 中完成 DB-first 历史交接，
然后注入下一次 Reasoning。客户端按 `USER_MESSAGE` 进入 MessageList、按
`session/queue` 完成最终清理，输入区不会因为等待 LLM 而暂停。

## 关键实现

- `InboundData` 增加 `PromptMode = Literal["queue", "steer"]`；WebSocket、Bus、Inbox
  共享同一字段，非法 mode 在 WebSocket 和 Inbox 两层拒绝。
- `SessionEventService.emit_user_message_if_absent()` 使用
  `sha256(session_id + request_id)` 生成稳定消息 id，先通过 SessionProjection/upsert
  落库，再发布 `USER_MESSAGE`。
- `InboxService.deliver_next_step_for_reasoning()` 顺序固定为：peek → before-claim →
  UserMessage upsert/echo → repository claim → queue snapshot；历史失败时 pending 不动。
- `SessionProjection` 在 active Reply 上建立 segment 边界；Session transcript 和客户端
  reducer 都保持“前半 assistant → Steering UserMessage → 后半 assistant”顺序。
- Steering event 的重复检查与 segment boundary 共用投影锁，避免并发重试把同一条
  Reply 切分两次；Session repository 的批量插入同样按 message id 幂等。
- 客户端 `QueueItemView` 保留 placement；队列横幅对 queued 项显示“插入当前运行”，
  steering 项显示“等待下一次推理”，只调用 `session.updateQueue({kind:"steer"})`，不
  乐观删除、不重复发送消息。

## 验证

| 仓库 | 命令 | 结果 |
|---|---|---|
| `E:\ftre` | `python -m pytest -q` | 527 passed（最终全量复跑） |
| `E:\ftre` | `python -m pytest -q tests/architecture tests/contracts tests/startup packages/ftre-inbox/tests` | 200 passed（最终专项复跑） |
| `E:\ftre` | `python -m ruff check src tests packages` | passed |
| `E:\ftre` | `git diff --check` | passed |
| `E:\binn\ftre-desktop` | `pnpm --filter @ftre/renderer test` | 514 passed / 52 files |
| `E:\binn\ftre-desktop` | `pnpm --filter @ftre/renderer exec tsc --noEmit` | passed |
| `E:\binn\ftre-desktop` | `pnpm --filter @ftre/renderer build` | passed（仅既有 Vite warnings） |
| 两仓 | WebSocket/Inbox/Hook/客户端 placement 专项 | passed |

## 文件与边界

后端改动集中在 Bus protocol、SessionEventService、ftre-inbox Package 和对应测试；
客户端改动集中在 renderer WebSocket client、chat projection、QueuedMessagesBanner 和
对应测试。没有修改 `E:\ftre-agent-core`、`E:\cordis-py`，没有把 QueueItem 或 Inbox
类型传入 Core，也没有新增 AgentService 队列 API。

## 工程卫生

本阶段未执行 commit、push、merge 或 release；执行前已有的工作区修改未被覆盖。构建生成
的 renderer `dist` 已被现有忽略规则排除，未加入提交范围。

## Refactor Cleanup Audit（2026-08-24）

### 范围与 Owner

| 能力 | 唯一运行时 Owner | 审计结论 |
|---|---|---|
| `queue → steer` 接入协议 | WebSocket Channel → `InboundData`/Bus protocol | `mode` 只在公开协议层归一化一次；没有第二份 steer 解析器 |
| pending、`next-turn`、`next-step`、claim | `ftre-inbox` `InboxService`/`InboxRepository` | 队列模型未进入 AgentService；`next-*` 是 Package 内部存储目标，不是旧兼容入口 |
| Steering UserMessage 落库与广播 | `SessionEventService` → `SessionProjection` | DB-first 顺序唯一；稳定 event id 和投影锁负责幂等 |
| Reply segment 顺序 | `SessionProjection` + `SessionRepository.insert_messages_after` | 原子完成“旧 assistant → UserMessage → 新 assistant”边界 |
| 客户端队列与历史交接 | renderer `chat.ts`/`QueuedMessagesBanner` | 只通过 `USER_MESSAGE` 交接，不乐观删除、不重复发送 |

### 引用、生命周期与旧实现检查

- F22 修改文件的 AST import 扫描未发现 `ftre.agent`、`ftre.api`、`ftre.bus`、
  `ftre.channel`、`ftre.command`、`ftre.tools`、`ftre.session`、`ftre.mcp` 或
  `ftre.plugin` 退役入口引用。
- Inbox Plugin 的 Hook、Worker、Repository 和依赖引用均绑定当前 Fiber/`service.close`；
  close 会取消 Worker、清理 receipt、释放依赖引用，并保留 pending 供恢复。
- Steering 投影的重复检查与原子 boundary 共用锁；Repository 批量插入按 message id 幂等。
- 未发现 F22 范围内单行转发壳、重复 Service key、全局 setter、静态 Service Bag 或第二个
  Composition Owner。`tests/architecture` 的旧 Hook 名称只用于“禁止出现”断言，不是生产引用。

### 最终门禁与清理

- 后端 `python -m pytest -q`：527 passed；专项 `tests/architecture tests/contracts tests/startup packages/ftre-inbox/tests`：200 passed。
- 后端 Ruff、`git diff --check`：通过；客户端 B3 514 passed、TypeScript、Vite build：通过。
- 两仓 `__pycache__`、`.pytest_cache`、`.ruff_cache` 清理后为 0。
- `E:\ftre\.ftre-inbox` 下的空 Session 目录是被忽略的运行时用户数据，未删除；
  Desktop 的 `.ftre/snapshot`、`.taskmaster/reports` 和后端日志目录同理保留，避免审计越权删除用户数据。
- 当前两仓仍为 feature 分支且存在本阶段未提交修改；未执行 commit、push、merge 或 release。
