# 执行提示词 04：F15.4 Session、Inbox 与 WebSocket 顺序收敛

你正在 `E:\ftre` 执行 F15.4。先核对 F15.1-F15.3 的提交、执行报告和测试。本批允许修改
Session、`packages/ftre-inbox` Hook 契约、WebSocket 消费者及相关测试；不得修改 Agent Core。

## 一、Session Hook

- 只保留 `session/created`、`session/disposed`，改为真正 awaited 的 PARALLEL/OBSERVE 事实通知。
- 删除 `session/event`、`session/flush` 的 Spec、DTO、dispatch、导出、测试和文档。
- Session create/dispose 的持久事实必须先提交，再通知观察者；listener 失败被诊断但不回滚事实。
- `session/disposed` 返回前，Inbox 等必要清理 listener 必须已结束。
- 不用空 Hook 冒充 Repository flush；未来若有多个真实 Store 屏障再单独立项。

## 二、Inbox Hook

将 6 个收敛为 3 个：

- 保留 WATERFALL `inbox/before-claim`；
- 保留 awaited `inbox/changed`、`inbox/status-changed`；
- 删除 `inbox/inserted`、`inbox/claimed`、`inbox/discarded` 的 Spec、DTO、dispatch 和导出。

队列 Store mutation 成功后才发布 changed；listener 读取 revision/snapshot 时必须看到已提交的
权威状态。不能改变 admission、claim、discard、next-turn/next-step、容量、恢复和持久格式。

## 三、WebSocket 顺序

- WebSocket 只监听 changed/status-changed，不 concrete import Inbox 私有 Store/Runtime。
- 连续 mutation、attach snapshot、status 变化和断连重连必须有确定顺序；旧 detached Task 不得迟到
  覆盖新 revision。
- 保持现有 `session/queue`、`session/status`、ACK/error envelope 和客户端字段不变。
- listener 必须绑定 WebSocket Plugin Fiber，unload/restart 后旧闭包不再接收事件。

## 四、测试与停止条件

使用 Event/Barrier 构造：Session dispose 等待、连续 revision、listener 失败、WebSocket 慢发送、
unload in-flight、restart 旧 listener 清零、pending 不丢/不重复。禁止长 sleep。

至少执行：

```powershell
python -m pytest -q packages/ftre-inbox/tests
python -m pytest -q tests/lifecycle tests/contracts tests/architecture
python -m pytest -q tests/test_session_json_store.py tests/test_gateway_runtime.py
python -m ruff check --no-cache src tests packages
git diff --check
```

清理旧 Hook 字符串、DTO、重复 Effect、缓存、临时 queue/db 和陈旧注释。更新执行报告、PRD 变更
记录和 TODO F15.4，按契约/实现/测试提交，不 push。停止时给出顺序不变量和第 05 批消费者列表。

