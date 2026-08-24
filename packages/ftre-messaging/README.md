# ftre-messaging

`ftre-messaging` 是独立的跨 Session 消息 Tool Plugin，拥有 `send_message`。

- `notify` 通过公开 MessageBus 通知目标 Session；
- `invoke` 通过注入的 `inbox` 接纳目标 Agent 输入；
- Inbox 只提供队列能力，不拥有本 Package 的 Tool；
- 卸载本 Plugin 只移除 `send_message`，不会关闭 Inbox 或 Agent。

安装后由 `ftre.plugins` entry point 发现：`ftre_messaging.plugin:apply`。
