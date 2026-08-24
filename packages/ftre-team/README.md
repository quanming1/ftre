# ftre-team

`ftre-team` 是独立的团队协作 Tool Plugin，拥有：

- `team_create`、`team_add_agent`、`team_say`；
- `team_agent_status`、`team_delete`、`wait_agent`。

团队关系写入公开 `SessionService` 的 metadata，成员执行通过 `AgentService` 和注入的
Inbox 完成。本 Package 不创建重复的内存 TeamService，也不由 Inbox Plugin 注册工具。
