# F24 Queue Operation Response 与 Inbox 快照统一协议执行报告

## 范围

- 仓库：`E:\ftre`
- 分支：`feature/F23-steering-message-boundary`
- 配套客户端阶段：`E:\binn\ftre-desktop` B5
- 未修改：`E:\ftre-agent-core`、`E:\cordis-py`、客户端 Electron 主进程
- 说明：工作区已有 F22/F23 迁移修改，本报告只记录 F24 新增的协议收敛和对应测试。

## Owner 与实现结果

| 责任 | 实现与证据 |
|---|---|
| Inbox revision | `packages/ftre-inbox/src/ftre_inbox/service.py:wire_snapshot` 返回持久化 `snapshot.revision`，不泄漏内部 next-turn/next-step |
| 操作响应 | `src/ftre/plugins/builtin/channels/websocket/channel.py:_send_queue_response` 在 durable mutation 后读取 wire snapshot，返回 `type/session/queue + request_id + ok + payload` |
| 后台广播 | `src/ftre/plugins/builtin/channels/websocket/plugin.py` 继续通过 Inbox changed Hook 广播无 request_id 的同一快照结构 |
| 错误 | `_reject` 保留 `request_id/ok=false/error`，快照读取异常返回 `queue_snapshot_failed`，不伪造成功响应 |
| 旧 ACK | `_send_admission_ack`、`value.accepted` admission 成功路径已删除；`_send_control_ack` 仅保留给不修改 Inbox 的 `session.cancel` |
| 幂等 | Inbox Repository 对 `(session_id, request_id)` 原子去重；既有 `test_duplicate_followup_does_not_create_receipt_after_original_completed` 与 steering duplicate 回归继续通过 |

## 协议示例

成功的 `session.prompt`、`session.updateQueue` 都只返回一帧：

```json
{
  "type": "session/queue",
  "request_id": "op-001",
  "ok": true,
  "payload": {
    "session_id": "ws_sess_1",
    "revision": 13,
    "items": []
  }
}
```

取消仍是独立控制 ACK，因为它不修改 Inbox 队列：这不属于 Queue Operation Response。

## 测试与质量门禁

已执行并通过：

```text
python -m pytest -q tests/startup/test_f12_ws_smoke.py tests/test_ws_volatile_replay.py tests/test_ws_control_commands.py packages/ftre-inbox/tests/test_service.py
24 passed
python -m ruff check src tests packages --no-cache
All checks passed
git diff --check
```

完整门禁在最终收尾重新执行，结果记录在本报告末尾。

最终门禁结果：

```text
python -m pytest -q
531 passed in 134.76s
python -m ruff check src tests packages --no-cache
All checks passed
git diff --check
passed
```

跨仓客户端门禁：

```text
pnpm --filter @ftre/renderer test
52 files, 517 tests passed
pnpm exec tsc -p packages/renderer/tsconfig.json --noEmit
passed
pnpm --filter @ftre/renderer build
passed（已有 CSS/chunk size 警告，无新增错误）
```

静态扫描确认生产代码不再包含 `_send_admission_ack`、`getMessageAckPayload`、
`QueueUpdateResult`、`MessageAckPayload` 或 `consumeDurableAdmissionAck`。测试/文档中保留的
`value.accepted` 仅属于 `session.cancel` 历史控制协议或验收说明，不属于 Inbox admission。

生成物清理：只清理了两个仓库源码范围内的 `__pycache__`、`.pytest_cache`、`.ruff_cache`、
`.vite` 和 renderer/electron 自有 `dist`；明确跳过 `node_modules`、`release`、`.git` 和用户数据
目录。剩余空目录均属于运行数据/依赖目录，未擅自删除。

## 变更记录

| 日期 | 结果 |
|---|---|
| 2026-08-25 | 建立 F24 PRD/TODO；统一后端 Queue Operation Response；增加 revision；客户端删除独立 admission ACK 与本地 Steering 猜测状态 |
| 2026-08-25 | 收尾审计补齐 steering 锁定：next-step 用户项在 claim 前拒绝 edit/remove；架构/WS 回归补测 | 修复仅靠客户端锁定、服务端可被旧/恶意帧绕过的边界债务 |
