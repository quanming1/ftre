# F12 执行报告：独立 Inbox Package 与权威队列协议

## 当前结论

F12 的 ftre 仓库内迁移、跨仓库命名冻结、Core `agent/before-reasoning` 接入和后端
WebSocket endpoint smoke 均已完成。桌面客户端未修改；其后续联调只需遵循本阶段冻结的
wire contract。

## 已完成

- 新增 `packages/ftre-inbox` 独立发行物：模型、Repository、Service、Provider、Hook、
  Cordis entry point、README、单元测试和独立 `pyproject.toml`。
- `next-turn` / `next-step`、`followup` / `steer` / `inject`、共享容量、request 幂等、
  原子 edit/remove/promote、批量候选和 at-most-once claim 已由 Package 唯一持有。
- 旧 `state.json.mailbox.pending` 可一次性迁移至独立 `inbox.json`；迁移幂等，损坏和
  重复 ID 保留旧事实并产生诊断日志；Session fork 不复制 Inbox。
- AgentService/AgentDriver 只暴露 `InboundMessage` 执行、active 取消和状态；旧
  `SessionLane`、`MailboxStore`、Session mailbox API、旧 mailbox payload 已删除。
- WebSocket 已冻结现代协议：`session.prompt`、`session.updateQueue`、`session.cancel`、
  `session/queue`、`session/status` 和统一 ACK/error envelope；不再接受旧
  `user_message`/`cancel`/`frame_id`/`mailbox_snapshot` 长期兼容路径。
- Command 在 AgentLoop ingress 旁路解析和执行，不进入 Inbox 或 TurnExecutor。
- Inbox/Agent/Session 生命周期、状态隔离、Hook failure/keep/discard、重启恢复和 WS
  基线测试已补齐。

## 验证证据

```text
python -m pytest -q                         -> 428 passed
python -m ruff check src tests              -> All checks passed
python -m ruff check packages/...           -> All checks passed
git diff --check                            -> passed
python -m pip wheel --no-deps --no-cache-dir packages/ftre-inbox packages/ftre-compaction
                                             -> ftre_inbox-0.1.0; ftre_compaction-0.1.0
Gateway runtime smoke                     -> STARTED / CANCELLED / CLEANUP OK
WebSocket endpoint smoke                  -> attach / prompt / steer / edit / remove / cancel / reconnect PASS
Core active-steer integration             -> ftre-inbox Hook + Core ReAct Tool→Reasoning PASS
```

## 2026-08-23 架构清理复审补充

本轮 `refactor-cleanup-audit` 复核了 F12 的 Owner、旧引用、生命周期和生成物：

- `ftre-inbox` unload/restart 会取消 worker、receipt，并清理 Agent、HookRuntime、状态发布
  和宿主闭包引用；Hook Runtime 绑定提供可逆 disposer。
- 必选 `sessions`、`agents`、`hook_runtime` 依赖改为显式注入；TurnExecutor 的 Inbox 能力由
  Provider 显式传入，不再通过 AgentLoop 动态查找。
- `AGENTS.md`、本目录 PRD 总览和本报告已明确区分 F12 当前协议与早期 SessionLane/Mailbox
  历史设计。
- 全量测试增补至 **428 passed**；独立 Core 全量 **238 passed**；专项测试、WebSocket endpoint 和 ftre-inbox active-steer 集成通过；ruff、vulture、TODO YAML、wheel、
  Gateway start/close smoke 和 `git diff --check` 均通过。
- 最后一次测试后已清理 `__pycache__`、`.pyc`、`.pytest_cache`、`.ruff_cache`、`build`、
  `dist`、`*.egg-info` 和空目录，审计范围内剩余数量均为 0。

## 2026-08-23 审计修复补充

- Inbox 仅为新接纳的 `followup`/`next-turn` 创建 Turn receipt；重复或已由 Session
  历史确认的 request 不会创建无法完成的 Future，`steer`/`inject` 继续只走上下文注入。
- `AgentLoop.stop()` 关闭 `CompletionRegistry`，清空缓存并唤醒 in-flight waiter；新增
  shutdown 回归测试，避免 Gateway 关闭后任务永久悬挂。
- 清理当前测试和包文档中的 `SessionLane`/`CompactManager` 旧 Owner 名称；历史迁移字段
  `legacy_mailbox` 仍保留并由一次性迁移测试覆盖。
- 复审后的 ftre 全量测试为 **428 passed**，Core 全量为 **238 passed**；Inbox 与
  Compaction wheel 均成功构建。

专项覆盖包括：

- `tests/architecture/test_f12_inbox_boundaries.py`
- `packages/ftre-inbox/tests/`
- `packages/ftre-inbox/tests/test_plugin_hook.py`
- `tests/startup/test_f12_ws_smoke.py`
- `tests/contracts/test_f9_command_ingress.py`
- `tests/lifecycle/test_f10_lifecycle_faults.py`
- `tests/test_ws_control_commands.py`
- `tests/test_ws_volatile_replay.py`

## 跨仓库 Step Hook 接入

本轮已冻结并实现以下边界：

- `agent/before-turn`：ftre AgentLoop 的一次 Turn 准入；
- `agent/before-reasoning`：Core 每次真实 LLM Reasoning 前的通用消息贡献 Hook；
- `inbox/before-claim`：ftre-inbox 的 pending claim 策略；
- `agent/after-turn`：ftre Turn 收尾维护屏障。

Core 不 import ftre 或 Inbox；ftre-inbox 监听 Core Spec，在 Hook 内原子领取 `next-step`
并返回普通 user messages。联合全量测试、Gateway runtime start/stop、真实 WebSocket
endpoint smoke 和 active-steer 集成证据均已通过；本阶段不修改桌面客户端。

## 2026-08-23 删除竞态修复

- `AgentLoop.delete_session()` 现在使用等待版取消：先终止并等待 active Turn 的消息投影、
  `PIPELINE_END` 和 Hook 收尾，再删除 Session 历史。
- `SessionProjection` 的 `REPLY_END` 只有在最终 Msg 快照成功持久化后才从 active 集合移除；
  持久化失败时仍可由 `finish_open()` 重试收尾，避免助手回复消失。
- Session 已删除时，状态发布会静默跳过，不再生成空 `to_channel` 的 ChannelManager 告警。
- 新增 `test_delete_session_waits_for_active_turn_before_removing_history`、
  `test_deleted_session_does_not_publish_status_to_empty_channel` 和
  `test_reply_end_keeps_snapshot_when_final_persist_fails`；ftre 全量测试为 **428 passed**，
  `ruff check src tests` 与 `git diff --check` 通过。
