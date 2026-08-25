# F25 执行报告：Compaction 三路并行摘要与确定性合并

## 范围

- 仓库：`E:\ftre`
- 分支：`feature/F25-parallel-compaction`
- 修改范围：`packages/ftre-compaction`、F25 PRD/TODO/CHANGELOG、一个因当前 Core 事件契约更新的 ftre 投影测试。
- 未修改：`E:\ftre-agent-core` 源码、Desktop、Inbox、WebSocket 字段协议和 Session 持久化结构。
- 未执行：commit、push、merge、release、Gateway 重启。

## 实现结果

### 1. 三路 Worker

`CompactionService._run_compact_llm()` 只序列化一次 Session 快照，然后并行调度：

- `intent`：`primary_request_and_intent`、`all_user_messages`；
- `technical`：`key_technical_concepts`、`files_and_code_sections`、`errors_and_fixes`；
- `continuity`：`problem_solving`、`pending_tasks`、`current_work`、`next_step`。

每个 Worker 使用同一压缩模型配置，但只输出自己的 XML 节点。三个结果由本地
`_merge_summary_parts()` 按固定顺序组装成唯一 `state_snapshot`，不引入第四个合并 LLM。

### 2. 失败、超时和取消

- 每个 Worker 默认独立重试一次；配置 `parallelRetryAttempts` 可设为 0–2。
- 每个 Worker 默认 120 秒超时，配置被限制在 5–300 秒。
- `parallelWorkers` 是并发度，限制在 1–3；默认 3，设为 1 可串行诊断。
- 任一分片最终失败时不发 `context_compact_done`；现有 `_do_compact()` 继续走
  `context_compact_failed` 和 `compress_fast` 兜底。
- 共享压缩 Task 被取消时，三个子 Worker 一并取消；等待者取消仍由既有 `asyncio.shield`
  语义保护共享 Task。

### 3. 协议和生命周期

`context_compact_start` 仅增加可选诊断字段 `mode=parallel`、`workers`；
`context_compact_done/failed`、SessionProjection、Hook 和客户端协议保持不变。
压缩仍由 `ftre-compaction` 唯一 Service/Plugin Owner 管理。

## 文档与配置

- `docs/prd/PRD-F25-parallel-compaction.md`：已验收，FR1–FR9、AC1–AC10 已勾选。
- `docs/TODO.yaml`：F25 及 F25.1–F25.5 标记 `done`。
- `packages/ftre-compaction/README.md`：补充三路分工、配置和失败语义。
- `CHANGELOG.md`：追加 F25 未发布条目。

配置示例：

```json
{
  "agents": {
    "context": {
      "parallelWorkers": 3,
      "parallelTimeoutSeconds": 120,
      "parallelRetryAttempts": 1
    }
  }
}
```

## 验证记录

| 命令 | 结果 |
|---|---|
| `python -m pytest -q packages/ftre-compaction/tests` | `44 passed` |
| `python -m pytest -q packages/ftre-compaction/tests tests/hooks tests/lifecycle tests/contracts` | `109 passed` |
| `python -m pytest -q` | `537 passed` |
| `python -m ruff check --no-cache src tests packages` | 通过 |
| `git diff --check` | 通过 |
| `python -m build --wheel --sdist`（`packages/ftre-compaction`） | wheel/sdist 均构建成功 |

新增专项覆盖：三路并发重叠、同一快照、分片独立重试、XML 节点归属/合并、缺节点拒绝和
三路统一取消。

## 已知边界

三路 Worker 默认都会携带同一份序列化上下文，因此墙钟时间降低但输入 token 成本可能增加。
本阶段选择质量优先和实现简单；后续可另立阶段为三个 Worker 提供按语义过滤的上下文视图，
降低输入成本，不在 F25 内扩大范围。

## 最终状态

- F25 PRD/TODO/CHANGELOG 与代码证据一致。
- 当前工作树保留本批未提交修改；未执行任何 Git 提交或外部发布操作。
