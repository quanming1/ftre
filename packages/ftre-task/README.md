# ftre-task

`ftre-task` 是独立的 Subagent 编排 Tool Plugin，拥有 `task`。

它负责创建/复用 subagent Session、通过注入的 Inbox 投递任务，以及按 request 精确等待
完成结果。Inbox 只负责队列接纳与消费，不拥有 `task` Tool。

卸载本 Plugin 只移除 `task`，不会关闭 Inbox、Agent 或 Session Service。
