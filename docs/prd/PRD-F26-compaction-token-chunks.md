# PRD-F26 Compaction 按 token 分块摘要

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F26 |
| 名称 | Compaction 按 token 分块摘要 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-25 |
| 定稿日期 | 2026-08-25 |
| 验收日期 | 2026-08-25 |
| 关联文档 | `docs/TODO.yaml` F26；`PRD-F11-compaction-gate-hook.md`；`PRD-F25-parallel-compaction.md` |

## 1. 背景与目标

### 1.1 背景

F25 的三路语义 Worker 都把同一份完整上下文发送给 LLM。上下文越长，三次请求的输入成本和
单次响应等待越大；压缩期间 Hook 必须等待所有 Worker，用户会明显感到卡顿。问题不在
“语义 Worker 数量”，而在每个 LLM 都重复处理整份历史。

### 1.2 目标

把待压缩内容按估算 token 数切成约 100k token 的消息块，每个块只交给一个 LLM 摘要，多个
块受并发上限约束并行处理，最后由本地确定性合并为一个 `state_snapshot`。一次压缩不再让
多个 LLM 重复读取同一份完整历史。

### 1.3 非目标

- 不改变 `agent/after-run`、`agent/run-error`、`inbox/before-claim` Hook 和 `/compact` 入口。
- 不修改 Agent Core、Inbox、WebSocket、Session Msg 或客户端协议。
- 不把每条消息强行截断成半条；分块优先保持 Msg 边界，单条超大 Msg 允许形成 oversized chunk 并记录日志。
- 不新增合并 LLM；分块结果只能由本地确定性逻辑合并。
- 不追求精确 tokenizer；继续使用 ftre 已有字符级 token 估算器作为分块依据。

## 2. 需求范围

### 2.1 功能需求

- [x] **FR1：固定 token 分块**：默认每块上限 `100000` 个估算 token；按原始 Msg 顺序累加，
  超过上限时从下一条 Msg 开始新块。
- [x] **FR2：单块单 LLM**：每个 chunk 恰好创建一次摘要 LLM 调用；Worker 不再按 intent、
  technical、continuity 三路重复发送完整上下文。
- [x] **FR3：受限并发**：chunk LLM 默认最多 4 个并行，块数超过并发上限时分批执行；每个块
  仍保持独立 request、超时和重试。
- [x] **FR4：滚动摘要**：只有第一个 chunk 接收上一轮 `previous_summary`，后续 chunk 只摘要
  自己的内容，避免旧摘要被每个块重复复制。
- [x] **FR5：结构化输出**：每个 chunk 输出已有 `state_snapshot` 节点集合的局部正文；允许
  某些节点为空，但至少要有一个有效节点。解析失败或空输出视为该 chunk 失败。
- [x] **FR6：确定性合并**：本地按固定节点顺序合并所有 chunk 的同名节点，保持下游 Agent
  继续读取 `state_snapshot` 的格式；不得引入第四个 LLM。
- [x] **FR7：失败安全**：任意 chunk 最终失败都不发 `context_compact_done`、不写半成品；沿用
  现有 `context_compact_failed` 和 `compress_fast` 兜底。
- [x] **FR8：取消与生命周期**：所有 chunk Task 属于同一个共享压缩 Task；取消/关闭必须等待
  全部子任务退出，不能留下后台 LLM 请求。
- [x] **FR9：诊断信息**：`context_compact_start` 增加 chunk 数、chunk token 上限和并发上限；
  Service 日志记录总块数、每块 token 数、模型、API 类型、尝试次数、耗时和结果。
  日志记录每个 chunk 的估算 token、耗时、重试和失败原因，不记录完整上下文或密钥。

### 2.2 非功能需求

- 性能：每个 LLM 只处理约 100k token，而不是重复处理完整 Session；并发墙钟时间接近最慢一批 chunk。
- 一致性：分块严格按 Msg 顺序；同一压缩快照只产生一个 done 事件。
- 成本：总输入量从“完整上下文 × Worker 数”降为“各 chunk 之和 + 第一个 chunk 的旧摘要”。
- 可配置：`chunkTokens`、`chunkParallelism`、`chunkTimeoutSeconds`、`chunkRetryAttempts` 均可配置并有上限。

## 3. 技术方案

### 3.1 模块变更

```text
packages/ftre-compaction/
├─ src/ftre_compaction/
│  ├─ config.py       # chunk token 上限、并发、超时、重试
│  └─ service.py      # Msg 分块、chunk LLM、确定性合并、失败/取消
└─ tests/
   ├─ test_config.py
   ├─ test_parallel_compaction.py
   └─ test_compact_summary.py
```

### 3.2 分块流程

```text
get_context_messages()
  → 去掉 leading compact Msg，得到本次 head
  → estimate_messages_tokens([Msg]) 逐条估算
  → 累加至 100k，按 Msg 边界生成 chunk[0..N]
  → chunk LLM（受 chunkParallelism 限制）
  → 按 chunk 顺序解析并合并 state_snapshot 节点
  → token 膨胀检查
  → 唯一 context_compact_done
```

### 3.3 分块输出

每个 chunk 使用同一套节点契约，但只写该块可证明的事实：

```xml
<state_snapshot>
  <primary_request_and_intent>本块相关内容</primary_request_and_intent>
  <key_technical_concepts>本块相关内容</key_technical_concepts>
  ...
  <next_step>本块相关内容</next_step>
</state_snapshot>
```

本地合并器把同名节点按 chunk 顺序用空行连接；空节点不产生额外文字。这样既保留现有摘要
格式，又不要求一个 LLM 了解其他 chunk。

## 4. 配置与事件

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

安全边界：`chunkTokens` 限制在 16k–1,000,000；`chunkParallelism` 限制在 1–8；超时限制在
5–600 秒；重试限制在 0–2 次。旧 F25 的 `parallelWorkers` 等配置不再作为新语义读取，避免
把“三路语义 Worker”概念继续带入实现。

`context_compact_start.value` 增加：

```json
{
  "mode": "token_chunks",
  "chunks": 4,
  "chunk_tokens": 100000,
  "parallelism": 4
}
```

已有事件名称和 `context_compact_done/failed` 数据协议不变。

## 5. 验收标准

- [x] **AC1**：低于 chunk 上限的内容只调用 1 个 chunk LLM；跨越上限的内容按 Msg 边界调用多个 chunk LLM。
- [x] **AC2**：chunk LLM 的输入不包含其他 chunk 的会话正文；第一个 chunk 可收到 previous summary，
  后续 chunk 不重复携带。
- [x] **AC3**：并发测试证明最多同时运行 `chunkParallelism` 个 LLM，结果按原 chunk 顺序合并。
- [x] **AC4**：输出仍是唯一完整 `state_snapshot`，同名节点顺序稳定，缺失节点不会生成 `None` 或重复标签。
- [x] **AC5**：任一 chunk 重试失败时没有 done/半成品，现有 fast fallback 正常执行。
- [x] **AC6**：共享 Task 取消、Plugin close 能清理所有 chunk 子任务。
- [x] **AC7**：自动 Hook、`/compact`、overflow recovery、现有客户端事件和 Session 持久化回归通过。
- [x] **AC8**：Package 专项测试、ftre 全量测试、ruff、Package build 和 `git diff --check` 通过。

## 6. 测试计划

- 分块边界：空输入、低于 100k、恰好 100k、跨越 100k、单条 oversized Msg。
- LLM 调度：调用次数、同一 chunk 输入隔离、并发上限、顺序合并。
- 滚动摘要：首块携带 previous summary，后续块不重复携带。
- 失败恢复：单块重试、最终失败、fast fallback、无半成品事件。
- 生命周期：取消共享 Task、Plugin close、无悬空子 Task。
- 回归：compact Msg 游标、消息在压缩期间到达、token 膨胀保护、普通 Agent Turn。

## 7. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-25 | 新建 F26，废弃 F25 的“三路语义 Worker 共享完整上下文”，改为每约 100k token 一个 chunk、每 chunk 一个 LLM | F25 重复发送完整历史，压缩墙钟时间和输入成本过高 |
| 2026-08-25 | 完成 token 分块、受限并发、配置边界、确定性合并、失败取消和全量回归；chunkTokens/chunkParallelism/chunkTimeoutSeconds/chunkRetryAttempts 均可配置 | 用户要求压缩策略按内容数量工作，并可调整具体参数 |
| 2026-08-25 | F27 接管默认 chunk 粒度和机械性用户消息节点；F26 的 100000 默认值由 F27 的 200000 取代 | 将输出优化与已验收的 token 分块阶段分开追踪 |
