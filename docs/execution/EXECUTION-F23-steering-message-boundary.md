# F23 执行报告：Steering 安全边界与多 AssistantMessage 持久化

## 结果

- 状态：已完成
- 分支：`feature/F23-steering-message-boundary`
- 范围：ftre Host、`ftre-inbox` Package、Session Projection、协议测试和文档；未修改客户端、Agent Core 源码或 cordis-py。

## 实现证据

| 语义 | 代码位置 | 结果 |
|---|---|---|
| 安全边界 | `packages/ftre-inbox/src/ftre_inbox/service.py` | `peek → before-claim → UserMessage upsert/broadcast → claim`，持久化失败保留 pending |
| UserMessage 稳定 id | `src/ftre/services/session/events.py`、`packages/ftre-inbox/src/ftre_inbox/plugin.py` | `session_id + request_id` 生成稳定 Msg id，并将同一 id 交给 Core |
| Agent 不持有队列 | `src/ftre/services/agent/runtime/engine.py`、`services/agent/contracts.py` | idle fallback 识别 `history_message_id`，不重复写 UserMsg；Agent 输入仍是 `InboundMessage` |
| Projection 按消息坐标 | `src/ftre/services/session/projection.py` | `_replies` 以 `message_id` 为 key，同 reply 的新消息自动封口旧 Assistant |
| 删除人工切分 | Session Service/Repository/Projection | 删除 `supports_steering_boundary`、`insert_messages_after()`、`_project_steering_user_message()` 及 `reply_id:segment:*` 生产路径 |
| Package 生命周期 | `packages/ftre-inbox/src/ftre_inbox/plugin.py` | Inbox 仍独立 provide/inject，Hook、Worker、Session 引用随卸载清理 |

## 验证

```text
python -m pytest -q
527 passed（含 SessionProjection A→User→B、Core→Projection 跨层、Inbox DB-first、失败保留 pending、WS smoke、Package Hook）

python -m ruff check src tests packages --no-cache
All checks passed
```

测试不依赖真实 LLM；通过 Core fake provider 和 Session/Inbox 内存/临时目录验证跨层
协议。Core C4 的全量 240 tests、Desktop B4 的 514 tests 与 TypeScript/build 作为
配套仓验收输入。

新增 `tests/test_f23_core_projection_integration.py`：直接运行 Core fake provider，
再交给 ftre SessionProjection，验证真实事件中的两个 `message_id` 最终持久化为
`Assistant A → User → Assistant B`。

## 收尾审计

- `src/`、`packages/`、`tests/` 中没有 `supports_steering_boundary`、`insert_messages_after()`、
  `_project_steering_user_message()`、`reply_segment` 或 `reply_id:segment:*` 的生产/测试引用。
- 审计修正 `ftre-inbox/plugin.py`：`session_events` 已声明为必选 `inject`，现在只通过
  `ctx.session_events` 读取，不再用动态 `ctx.get()` 查找。
- 全量回归发现两个独立 Inbox Hook 测试没有填充该必选依赖；已在
  `packages/ftre-inbox/tests/test_plugin_hook.py` 的最小 Cordis fixture 中显式提供
  `session_events=None`，不改变生产行为，也不重新引入隐式 Service Locator。
- 复跑后后端全量为 527 passed；Core 240 passed、Desktop 514 passed，三端专项门禁仍通过。
- 已删除本次验证生成的 ftre `__pycache__`、`.pytest_cache` 和 `.ruff_cache`；复核数量为 0。
- 工作树仍保留本批未提交的源码、PRD、TODO、CHANGELOG 和执行报告修改；未执行 commit/push。

## 失败与恢复语义

| 时刻 | 结果 |
|---|---|
| LLM/Tool 尚未结束 | U 保持 Inbox pending，Core 继续 Assistant A |
| Assistant A checkpoint 完成 | 才允许 UserMessage U 落库和广播 |
| U 落库失败 | 不 claim，重试仍可幂等落库 |
| U 落库成功但 claim 前崩溃 | 历史已有 U，Inbox 仍 pending，重启可继续 claim |
| claim 后 Core 首个 B 事件 | Projection 以 `message_id=B` 新建 Assistant，历史为 A→U→B |
