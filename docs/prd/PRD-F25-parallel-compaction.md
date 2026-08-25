# PRD-F25 Compaction 三路并行摘要与确定性合并

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F25 |
| 名称 | Compaction 三路并行摘要与确定性合并 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-25 |
| 定稿日期 | 2026-08-25 |
| 验收日期 | 2026-08-25 |
| 关联文档 | `docs/TODO.yaml` F25；`PRD-F11-compaction-gate-hook.md`；AGENTS.md |

## 1. 背景与目标

### 1.1 背景

当前 `ftre-compaction` 在 `CompactionService._do_compact()` 中读取一次 Session 快照，
然后通过 `_run_compact_llm()` 串行等待一个 LLM 生成完整 `state_snapshot`。压缩发生在
`agent/after-turn`、`agent/request-error` 或 Inbox 领取前的维护屏障中，整个 Session 会等待
该调用完成；长上下文和复杂摘要会让用户明显感到压缩缓慢。

### 1.2 目标

在不改变 Hook、Session、WebSocket 和客户端协议的前提下，将一次完整摘要拆成三个互不重叠
的语义分片并行生成，再在本地确定性合并为一个完整摘要，降低压缩墙钟时间并保持现有持久化
边界和失败安全性。

### 1.3 非目标

- 不修改 `ftre-agent-core`、Agent Hook 名称或 Core LLM 协议。
- 不修改 `ftre-inbox`、队列 claim、pending/steering 语义。
- 不修改客户端、WebSocket 事件名称或 `context_compact_done` 数据协议。
- 不引入第四个“合并 LLM”；合并必须是本地确定性逻辑。
- 不在第一版引入三个不同供应商的模型配置；三个 Worker 默认复用同一压缩模型配置。
- 不把压缩改成后台无屏障任务；维护 Hook 仍等待共享压缩 Task 完成。

## 2. 需求范围

### 2.1 功能需求

- [x] **FR1：单快照并行**
  - 每次压缩只读取一次 `get_context_messages()` 和 `through_message_id`。
  - 三个摘要 Worker 使用同一份不可变上下文快照，不得分别读取 Session，避免分片看到不同历史。

- [x] **FR2：三路语义 Owner**
  - Worker A 只负责 `primary_request_and_intent`、`all_user_messages`。
  - Worker B 只负责 `key_technical_concepts`、`files_and_code_sections`、`errors_and_fixes`。
  - Worker C 只负责 `problem_solving`、`pending_tasks`、`current_work`、`next_step`。
  - 每个 Worker 只能输出自己负责的节点正文，不输出完整 `state_snapshot` 或其他 Worker 的节点。

- [x] **FR3：并行调用**
  - 使用 `asyncio.gather` 并行启动三个 LLM stream；默认并行度为 3。
  - 三个调用使用当前压缩配置选定的同一模型、API 类型、reasoning effort 和温度。
  - 配置允许将并行 Worker 数降为 1 进行诊断；默认值仍为 3。

- [x] **FR4：确定性合并**
  - 本地按照固定节点顺序组装一个 `<state_snapshot>`。
  - 合并结果必须保留现有节点名称和下游 Agent 可读格式。
  - Worker 输出中若带有自己负责节点或外层 `state_snapshot`，合并器应提取正文，避免重复嵌套。

- [x] **FR5：滚动摘要**
  - 已有 `previous_summary` 仍参与本次摘要；每个 Worker 只接收与自身节点相关的历史摘要部分。
  - 没有旧摘要时正常生成首个摘要；已有摘要时不得重复生成历史节点的第二个 Owner。

- [x] **FR6：分片重试与失败安全**
  - 每个 Worker 失败或空输出时独立重试一次，重试仍使用同一快照。
  - 任意 Worker 最终失败时，不发送 `context_compact_done`，不写入半成品摘要。
  - 全部 Worker 失败时沿用当前 `compress_fast` 兜底和 `context_compact_failed` 事件。

- [x] **FR7：摘要体积保护**
  - 合并后继续使用现有 `tokens_after >= tokens_before` 保护。
  - 合并结果为空、节点缺失或超出安全上限时视为失败，不推进 compaction generation。

- [x] **FR8：生命周期与取消**
  - 三个子调用属于当前共享压缩 Task；等待者取消不得取消共享 Task。
  - `CompactionService.cancel_compact()`、`cancel_all_compact_tasks()`、`close()` 必须取消并等待所有子调用。
  - 任一子调用取消后，其余子调用不得继续产生持久化副作用；只有主 Task 统一发事件。

- [x] **FR9：现有入口不变**
  - `/compact`、自动压缩、Inbox before-claim 和 overflow recovery 继续调用公开 `CompactionService`。
  - `agent/after-turn` 维护状态、`context_compact_start/done/failed` 事件和客户端展示语义保持不变。

### 2.2 非功能需求

- **性能**：三路调用并行，压缩摘要调用耗时以最慢 Worker 为主，而不是三个调用时延累加。
- **一致性**：所有 Worker 看到同一快照；一次压缩最多产生一个 `context_compact_done`。
- **成本可控**：默认只增加并发调用，不新增模型类型；每个分片有独立输出预算，避免摘要无限膨胀。
- **可诊断**：日志记录 session、Worker 名称、耗时、重试次数和失败原因，不记录 API key 或完整敏感上下文。
- **兼容性**：不改变现有 Session Msg、Hook、WebSocket 和客户端协议；未启用压缩 Package 的 Host 行为不变。

## 3. 技术方案

### 3.1 模块变更

```text
packages/ftre-compaction/
├─ src/ftre_compaction/
│  ├─ config.py       # 并行 Worker、超时、分片重试配置
│  ├─ service.py      # 快照、三路调度、合并、校验和统一落盘事件
│  ├─ hooks.py        # 不变：继续等待 CompactionService
│  └─ commands.py     # 不变：/compact 仍调用公开 Service
└─ tests/
   ├─ test_config.py
   ├─ test_compact_summary.py
   └─ test_parallel_compaction.py
```

### 3.2 Worker 输出契约

```python
SummaryPart(
    worker="technical",
    sections={
        "key_technical_concepts": "...",
        "files_and_code_sections": "...",
        "errors_and_fixes": "...",
    },
)
```

Worker 只返回正文映射；Service 负责把映射组装为：

```xml
<state_snapshot>
  <primary_request_and_intent>...</primary_request_and_intent>
  <key_technical_concepts>...</key_technical_concepts>
  <files_and_code_sections>...</files_and_code_sections>
  <errors_and_fixes>...</errors_and_fixes>
  <problem_solving>...</problem_solving>
  <all_user_messages>...</all_user_messages>
  <pending_tasks>...</pending_tasks>
  <current_work>...</current_work>
  <next_step>...</next_step>
</state_snapshot>
```

### 3.3 失败和取消边界

```text
CompactionService._do_compact
  ├─ 读取一次快照
  ├─ emit context_compact_start
  ├─ gather(worker A, worker B, worker C)
  │    └─ 每个 Worker 最多独立重试一次
  ├─ 任一失败 → failed + compress_fast 兜底，不写 summary Msg
  └─ 全部成功 → 本地 merge + token 校验 → 唯一 context_compact_done
```

共享 `_compact_tasks[session_id]` 仍是并发 Owner；三路 Worker 不单独注册 Service、Hook 或
Session 任务，不产生额外持久化事件。

## 4. 接口与配置

### 4.1 配置

配置归 `agents.context`，同时支持 camelCase 和 snake_case：

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

安全边界：`parallelWorkers` 限制在 1–3；超出或非法值回退 3；超时和重试次数使用非负
值并设上限，避免配置造成无限任务。

### 4.2 对外事件

不新增事件名称，不改变已有字段。`context_compact_start` 可在 `value` 中增加非必需诊断字段：

```json
{
  "mode": "parallel",
  "workers": 3,
  "model": "summary-model"
}
```

现有客户端只读取 `model`/token 字段，因此新增诊断字段不改变客户端协议。

## 5. 验收标准

- [x] **AC1**：单元测试证明同一压缩快照只触发三个并行 Worker，最大耗时接近最慢 Worker 而不是三者累加。
- [x] **AC2**：三路输出分别只包含自己的 XML 节点，最终只产生一个完整 `state_snapshot`。
- [x] **AC3**：previous summary、首个摘要、三路节点顺序和空节点均有回归测试。
- [x] **AC4**：一个 Worker 首次失败、重试成功时整体成功；重试后仍失败时不写半成品并执行现有 fast fallback。
- [x] **AC5**：取消等待者、取消共享 Task、Plugin close 都能等待并清理三个子调用。
- [x] **AC6**：合并摘要膨胀、空输出、非法节点时不发送 done 事件，generation 不推进。
- [x] **AC7**：`/compact`、自动 Hook、overflow recovery 继续使用同一 Service 入口，现有事件顺序不变。
- [x] **AC8**：`python -m pytest -q packages/ftre-compaction/tests` 全部通过（44 项）。
- [x] **AC9**：根仓 ruff 和 `git diff --check` 通过。
- [x] **AC10**：ftre 全量测试（537 项）、专项测试和 Package wheel/sdist 构建通过；未修改 Core、客户端或 WebSocket 协议。

## 6. 测试计划

- 配置解析：默认 3、非法值回退、1 Worker 降级、超时/重试上限。
- Worker 调度：fake adapter 记录开始时间，验证三路并发和同一上下文快照。
- 输出合并：节点归属、顺序、外层 XML 去重、空节点和 malformed 输出。
- 失败恢复：单路失败重试、全路失败、fast fallback、无半成品 compact Msg。
- 生命周期：共享 Task、等待者取消、主 Task 取消、Plugin close 后无悬空 Worker。
- 回归：已有 compact summary、rolling summary、message arriving during compact、token 膨胀保护。
- 手动：Gateway 运行中触发 `/compact`，观察 `context_compact_start → done/failed` 事件仅出现一套。

## 7. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-25 | 创建并定稿 F25；将单次完整摘要改为三个语义分工 Worker 并行，采用本地确定性合并和分片失败安全 | 当前压缩 Hook 等待单个 LLM 完成，长上下文压缩墙钟时间过长；不能通过修改 Core 或客户端解决 |
| 2026-08-25 | 完成三路 Worker、配置边界、合并校验、独立重试、统一取消和回退测试；Package 44 项与 ftre 537 项全量验证通过 | 证明并行化没有改变 Session/Hook/协议边界，并确保任一分片失败不会写入半成品摘要 |
