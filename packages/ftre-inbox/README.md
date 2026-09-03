# ftre-inbox

`ftre-inbox` 是 Gateway 的持久消息队列 Plugin/Package。它只做输入接纳和交付：

- `next-turn` / `next-step` 双队列和共享容量；
- `next-step` 只按 Session 排队，不绑定某一次 Run；同一 Session 的 reasoning 边界或正常完成后的 FIFO worker 都可以交付它；
- `followup`、`steer`、`inject` 三种写入语义；
- JSON 原子持久化、幂等 admission、`peek → Hook → claim` 和旧 `mailbox.pending` 一次性迁移；
- 每 Session 一个 FIFO worker、队列编辑/删除/提升和 `session/queue` 权威投影。
- 不拥有 `send_message`、`task` 或 Team Tool；这些能力分别由
  `ftre-messaging`、`ftre-task`、`ftre-team` Package 通过 `inject("inbox")` 消费。

claim 后消息从 pending 永久移除，Inbox 不负责 lease、release、retry、fallback 或 Agent
运行状态机；交付失败由 AgentService 返回终态，下一次重试必须产生新的 request。

只有 `followup`/`next-turn` 会创建可等待的本进程 Turn receipt；`steer`/`inject`
在 active Turn 的 Runtime Hook 中作为上下文消息消费，不承诺独立的 Turn 结果。

宿主注入 `AgentService`；Inbox 在内部把 `QueueItem` 转换为 `AgentRunRequest`，调用
`AgentService.run(agent_id, request)`，并可通过 `ftre_inbox.plugin:apply` 作为 Cordis
Plugin 装载。Agent 不需要知道 `QueueItem`、
pending、容量或队列目标；包卸载时已持久化但尚未 claim 的输入仍可在下次启动恢复。

基础导入不要求完整 Gateway：

```python
import ftre_inbox
from ftre_inbox.models import QueueItem
```

在 ftre Gateway 中，`ftre-inbox` 通过 `inbox/before-claim` Hook 与可选的压缩/策略
Plugin 协作，通过 `AgentService.run(InboundMessage)` 交付已 claim 输入。
