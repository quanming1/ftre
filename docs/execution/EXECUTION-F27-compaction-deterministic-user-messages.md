# F27 Compaction 用户消息确定性生成执行记录

## 范围

- 仓库：`E:\ftre`
- 阶段：F27
- 分支：`develop`（遵循当前用户指令，未提交）
- 只修改 `ftre-compaction` Package、测试、文档和配置说明。

## 实现

1. LLM 摘要节点列表移除 `all_user_messages`；模型不再机械复述所有用户输入。
2. Service 从本次压缩快照提取 `role=user && name=default` 的真实消息，跳过 `compact`、
   `compact_fast` 和隐藏摘要，按历史顺序生成 `all_user_messages`。
3. 增量压缩会读取上一份 `state_snapshot` 中的 `all_user_messages`，再追加当前 tail，避免
   摘要游标推进后丢失早期用户消息。
4. 默认 `chunkTokens` 调整为 `200000`，显式配置仍支持 16K–1M 边界。
5. `context_compact_start` 增加本次确定性生成的 `user_messages` 数量；日志不记录正文或密钥。

## 验证

- `python -m pytest -q packages/ftre-compaction/tests` → 50 passed
- `python -m pytest -q` → 544 passed
- `python -m ruff check --no-cache src packages tests` → 通过
- `python -m build --no-isolation packages/ftre-compaction` → wheel/sdist 成功
- `git diff --check` → 通过

## 影响

- `state_snapshot` 仍保留 `all_user_messages`，下游 Agent 和客户端协议不变。
- LLM 输入节点减少一个，默认块更大；并发、失败回退、取消和 Hook 生命周期不变。
