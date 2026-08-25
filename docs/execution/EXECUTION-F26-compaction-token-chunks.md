# F26 执行报告：Compaction 按 token 分块摘要

## 当前状态

- 仓库：`E:\ftre`
- 阶段：F26
- 状态：已验收
- 范围：`packages/ftre-compaction`、配置文档、测试、F26 PRD/TODO/CHANGELOG
- 未修改：Agent Core、Desktop、Inbox、WebSocket、Session 持久化协议

## 已实现

1. 删除 F25 的三路语义 Worker 共享完整上下文方案。
2. 使用 `estimate_messages_tokens()` 按 Msg 边界切块，默认每块约 100k token；单条超限消息
   保持完整并独立成为 oversized chunk。
3. 每个 chunk 只调用一个摘要 LLM；chunk 数量不再固定为 3。
4. 增加受限并发、单 chunk 超时、单 chunk 重试和共享取消清理。
5. 只有第一个 chunk 携带上一轮 `previous_summary`，后续 chunk 不重复发送旧摘要。
6. 每个 chunk 输出已有 `state_snapshot` 节点的局部内容，本地按 chunk 顺序合并同名节点。
7. `context_compact_start` 增加 `mode=token_chunks`、`chunks`、`chunk_tokens`、`parallelism`。
8. 日志输出总块数、每块 token 数、实际模型、API 类型、尝试次数、耗时和结果，不输出正文或密钥。

## 可配置参数

配置位于 `agents.context`，支持 camelCase 和 snake_case：

```json
{
  "agents": {
    "context": {
      "chunkTokens": 100000,
      "chunkParallelism": 4,
      "chunkTimeoutSeconds": 120,
      "chunkRetryAttempts": 1
    }
  }
}
```

安全边界：chunk token 为 16k–1M，并发为 1–8，超时为 5–600 秒，重试为 0–2 次。

## 验证记录

| 命令 | 结果 |
|---|---|
| `python -m pytest -q packages/ftre-compaction/tests` | 47 passed |
| `python -m pytest -q packages/ftre-compaction/tests tests/hooks tests/lifecycle tests/contracts` | 113 passed |
| `python -m pytest -q` | 541 passed |
| `python -m ruff check --no-cache src tests packages` | 通过 |
| `python -m build --no-isolation --wheel --sdist --outdir build/f26-package packages/ftre-compaction` | wheel/sdist 成功 |
| `python -m ruff check --no-cache packages/ftre-compaction/src packages/ftre-compaction/tests` | 通过 |
| `git diff --check` | 通过 |

## 验收结论

F26 的功能、配置、失败安全、生命周期和质量门禁已完成。根仓隔离环境的 `python -m build`
在创建临时环境时因当前网络无法获取 `setuptools` 失败，但 Package 使用现有构建环境执行
`--no-isolation` 的 wheel/sdist 均成功；该网络问题不影响代码或 Package 构建产物。
