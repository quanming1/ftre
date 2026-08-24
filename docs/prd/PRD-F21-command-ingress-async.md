# PRD-F21 Command 接入异步化与消息入口隔离

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F21 |
| 名称 | Command 接入异步化与消息入口隔离 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-24 |
| 定稿日期 | 2026-08-24 |
| 验收日期 | 2026-08-24 |
| 关联文档 | `docs/TODO.yaml` F21；`AGENTS.md`；`docs/PROCESS.md` |

## 1. 背景与问题

`messaging/route` 由 MessageBus 的单一 inbound consumer 串行消费。旧的 Command Plugin
在识别到 `/compact` 等命令后直接 `await CommandService.dispatch_inbound()`，而命令 Handler
可能执行 LLM、磁盘或长时间 Agent 控制操作。命令未完成前，后续普通消息无法进入 Inbox，
客户端只能保留本地 optimistic 队列；刷新页面后临时状态消失，消息会在命令完成后延迟出现。

本阶段只修复接入调度边界，不改变 CommandResult、Inbox 协议、压缩算法或客户端协议。

## 2. 目标与非目标

### 2.1 目标

- Command 接入在确认“已接管”后立即返回 durable admission ACK。
- 慢命令在 CommandService 自己的后台 Task 中执行，不占用 MessageBus inbound consumer。
- 普通文本仍由后续 Inbox Listener 接管，命令不会进入 Inbox 或 Agent Turn。
- 同一 `request_id` 在执行中或成功完成后只执行一次。
- Command Plugin 卸载时取消并排空后台任务。

### 2.2 非目标

- 不把 Command 变成 Agent 消息；
- 不改变 `/compact`、`/compress-fast` 的业务实现；
- 不修改 ftre-desktop、ftre-agent-core、cordis-py；
- 不增加第二个 MessageBus、Inbox 或 Command Owner。

## 3. 技术方案

1. `CommandService.submit_inbound()` 接收已经解析的 `CommandDef`，创建后台 Task 并立即返回
   `bool` 接纳结果；已有 `dispatch_inbound()` 继续作为需要等待结果的内部 API。
2. Command Plugin 的 `messaging/route` Listener 对已识别命令调用 `submit_inbound()`，通过
   `_accepted()` 返回统一 `IngressResult`；Handler 结束后的文本通过已有 `session/command`
   outbound Envelope 发布。
3. CommandService 保存后台 Task、in-flight request_id 和成功完成 request_id；断线重试只确认
   已接管，不重复调度 Handler。
4. CommandService 的 close Effect 取消后台任务并等待排空；未知 slash command 仍同步快速拒绝，
   普通文本仍调用 `next_()` 进入 Inbox。

## 4. 验收标准

- [x] AC1：慢 Command Handler 运行期间，`submit_inbound()` 立即返回，且 Handler 仍在后台执行。
- [x] AC2：同一 `request_id` 在 in-flight 和成功完成状态下不会重复执行。
- [x] AC3：真实 `messaging/route` Hook 对慢命令立即返回 ACK，普通消息不被命令阻塞。
- [x] AC4：Command Plugin unload/Composition close 会取消后台 Task，不残留未回收 Task。
- [x] AC5：`python -m pytest -q`、`python -m ruff check --no-cache src tests packages`、
  `git diff --check` 全部通过。
- [x] AC6：Gateway WebSocket smoke 中普通消息 durable ACK 延迟小于 1 秒；验证结果为 0.007 秒。

## 5. 测试计划与结果

- `tests/contracts/test_f9_command_ingress.py`：慢命令后台执行、request_id 幂等、真实
  `messaging/route` 接入 ACK 回归。
- 全量测试：512 passed。
- 专项命令测试：7 passed。
- Gateway WebSocket smoke：ACK 0.007 秒，测试会话已清理。
- Ruff 与 diff check：通过。

## 6. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-24 | 新增 F21：Command 接入改为后台提交，MessageBus 只等待接纳 ACK；增加幂等和生命周期测试 | 修复 `/compact` 等慢命令阻塞全局 inbound consumer，导致普通消息长时间停留客户端队列的问题 |
