# 执行提示词 04：F14.4 Messaging Ingress 与 Command/Inbox Plugin 交接

你正在 `E:\ftre` 执行 F14.4。目标是让“这条消息是不是 Command、是否进入 Inbox”在接入平面
完成，AgentService 只看已经交付的 `InboundMessage`。实现必须简单：复用已有 Bus envelope 和
`IngressResult` 语义，不新增 AgentControlPort、IngressCoordinator 或一串结果转换类型。

## 一、前置检查

- 阅读强制文档、F14 PRD、执行报告及 F14.3 后的实际 Agent API。
- 确认前三批已验证并提交，当前无未知修改。
- 追踪 WS/HTTP/Subagent → MessageBus → 当前 consumer → Command/Inbox → ACK/Outbound 的真实链路。
- 阅读 `ftre-inbox` 的 Plugin、worker、Hook 和 wire 代码；Host 不得 import 它的 concrete 类。

## 二、协议设计（必须遵守）

1. Messaging Owner 定义一个 transport-neutral inbound HookSpec。
2. 复用现有 Bus inbound message 和现有 `IngressResult` 语义；把结果契约移到 Messaging Owner。
   Hook listener 返回 `IngressResult | None`：`None` 表示“不属于我”，第一个非空结果结束分发。
3. Command Plugin：只识别/执行 slash command，返回结果并旁路 Inbox/Agent。
4. Inbox Package：只接纳普通输入和自己拥有的 cancel/updateQueue 语义，durable 成功后返回 ACK。
5. 没有任何 Plugin 处理时，Messaging 返回稳定、可诊断的 capability error。
6. MessageBus 负责 request/reply correlation 和 inbound/outbound transport，不保存 pending、不执行
   Command、不调用 Agent private Runtime。

若当前 HookRuntime 不支持“首个 handled 结果”，扩展通用 waterfall/first-result 机制必须保持业务
零知识；不得在 Kernel 写 Command/Inbox 分支。

## 三、实施步骤

- 将 inbound consumer 从 Agent Runtime 移到 Messaging Service/Provider 生命周期；
- 由 Messaging 发布 Hook，Command/Inbox 分别在自己的 Plugin Fiber 注册监听者；
- 移除 Agent 中的 `_consume`、`_parse_ingress_command`、`_dispatch_command_direct`、
  `bind_inbox`/`bind_inbound_handler` 等接入职责；
- Inbox worker 继续 Inject `agents` 并调用 `AgentService.run(InboundMessage)`，不泄漏 QueueItem；
- Command result、Completion/ACK、Session event 与客户端现有 wire 行为保持一致；
- 取消不伪装成 `/cancel` 文本，Command 不创建 Turn，未知 slash command 不进入 Agent；
- restart 后 Messaging dispatch 必须解析当前 Plugin listener，不能保留旧 Inbox/Command 闭包。

## 四、注释规范

- 在 inbound HookSpec 附近用中文解释 `None`/handled、监听顺序和 fail-closed 语义。
- 在 Command/Inbox listener 解释“为什么它在 Agent 之前裁决”，不要重复 if 条件。
- 对 ACK 与 durable admission、request_id 与 BusMessage.id 的差异保留准确注释。
- 删除 Agent 内旧分流注释，避免文档继续声称 AgentLoop 消费 Bus。

## 五、测试矩阵与代码清理

必须覆盖：

- 普通消息 → Inbox ACK → claim → AgentService；
- Command → CommandResult，Inbox/Agent 均未调用；
- 未知 slash command → command unavailable，不进入 Agent；
- 无 Command Plugin；无 Inbox Package；两者都无；
- duplicate request_id、queue full、cancel active/pending、Session 删除；
- listener unload/restart 和 in-flight dispatch；
- WS request/reply correlation、outbound command message、queue/status/history 不回归；
- Agent 源码无 Command/Queue/Bus consumer 业务分流。

执行 Messaging/Command/Inbox/WS/architecture/lifecycle 专项、全量 pytest、ruff、diff check。
扫描并删除旧 consumer、旧 bind、重复 result、兼容分支、缓存和空目录。

## 六、收尾与停止条件

- 接入分流唯一 Owner 是 Messaging Hook + 对应 Plugin listener；
- AgentService/Runtime 完全不知道 Command/Queue；
- Host 不 concrete import `ftre_inbox`；
- 客户端 wire 与 durable admission 语义有测试证据；
- 更新 PRD、执行报告和 TODO `F14.4`，分批提交、不 push、工作树干净；
- 汇报消息链路前后图、Hook 契约、删除项、测试与提交。
