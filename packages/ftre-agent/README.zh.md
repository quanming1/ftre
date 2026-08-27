# ftre-agent

ftre 平台的 Agent 稳定契约包。

本包是 ftre Agent 拆分（PRD-F33）中"只含契约"的一半：

- `AgentService` —— 公开 `agents` Service 入口（`run` / `cancel` / `status` / `is_busy` / `delete_session` / `resume_confirmation`）
- `InboundMessage` —— 唯一执行输入，由 Inbox 包完成 admission 后生成
- `AgentRunResult` —— 唯一执行结果（`completed` / `cancelled` / `failed`）
- `AgentRegistry` + `HookScopeCarrier` —— Agent 身份与 Hook 作用域载体
- Agent Hook（`agent/before-run`、`agent/after-run`、`agent/run-error`），并 re-export Core 拥有的 `agent/before-reasoning` / `agent/stop-decision`
- `AgentConfig` / `LLMConfig` —— Hook、Runtime 与压缩包共享的冻结配置快照

本包刻意不包含 AgentLoop、LLM Client 或任何工具执行实现。具体 Runtime 位于
[`ftre-agent-runtime`](../ftre-agent-runtime/)：它依赖本包，并通过 Provider
Plugin 发布 `agents` Service。

## 依赖边界

`ftre-agent` 不 import `ftre.services.*`，可独立安装给测试替身或其他 Host
使用。`~/.ftre/config.json` 的磁盘配置加载仍由 ftre Host 持有。
