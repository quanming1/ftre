# PRD-F43 服务端会话事件管道（v5，SessionLog 存储与 Token 修订）

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收
> **v4 已完成；本版为 v5 增量修订**：SessionProjection 及其继任方案（fold 返回值/ProjectionOutcome）
> 全部退场，改为 DSH 验证过的基础组合：SessionLog + write-behind + repair +
> deriveMessages；checkpoint 只由 `SessionService.flush_log()` 提供语义边界，
> 不再保留独立的 checkpoint 策略模块。**无旧数据兼容**。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F43 |
| 名称 | 服务端会话事件管道（SessionLog / 持久化 / 恢复 / 派生） |
| 状态 | 开发中（缺陷修复回归） |
| 创建日期 | 2026-09-04 |
| 定稿日期 | 2026-09-04 |
| 验收日期 | 2026-09-04 |
| 关联文档 | `docs/prd/PRD-F41-downstream-wire-protocol.md`（事件表/帧表契约）、`docs/prd/PRD-F42-client-session-lifecycle.md`、`docs/prd/PRD-F35-agent-service-inbox-message-boundary.md` §13（UserMessage 幂等语义在新架构下重述，见 §3.6） |

## 1. 背景与目标

### 1.1 v4 服务端形态

v3 的方案是"投影类内部拆分"（FoldOutcome/ProjectionOutcome/ActiveReplies/
reply_persistence）；v4 采纳 DSH 模式后这些中间结构**全部不需要**：

| v3 方案组件 | v4 替代 | 为什么不再需要 |
|---|---|---|
| `Msg.append_event` 改造返回 FoldOutcome | **删除**（服务端不再折叠） | 唯一 fold 场景是读侧 deriveMessages（纯函数，作用于全量日志） |
| `ProjectionOutcome{changes,deltas,sealed}` | **删除** | wire 是事件透传，无"变更"中间结构 |
| `ActiveReplies` 内存注册表 | **删除** | "进行中的回复"= 日志里的 chunk 事件本身 |
| `reply_persistence`（锁/恢复/幂等/落盘编排） | write-behind + repair 两个正交模块 | 落盘节奏与崩溃修复解耦，DSH 已验证 |
| `IMMEDIATE_CHECKPOINT_TYPES` | `SessionService.flush_log()` 窄方法 | 语义边界由 Service 暴露，Runtime 只依赖注入的 flush 能力 |
| state.json `messages:[Msg]` | session.jsonl 事件日志 | 唯一事实源 |
| `SessionEventService.emit` 两段式 | `SessionLog.append` + 转发器 | 提交即广播，无投影阶段 |

### 1.2 目标

实现 F41 事件表的生产端：SessionLog（提交/seq/校验/冻结）、write-behind 持久化、
`SessionService.flush_log()` 语义边界、崩溃 repair、deriveMessages 读侧、事件收敛映射（旧 21 事件 → 新 13）、
以及旧组件退场清单。v5 修复两个生产问题：流式 chunk 与累计 Assistant 快照造成的
`session.jsonl` 膨胀，以及 `turn/end` 累计 Token 覆盖最近一次调用 Token 导致的错误压缩。

### 1.3 非目标

- 不迁移旧 state.json（视为已删除，启动时忽略旧目录）。
- 不做 event sourcing 的高级能力（快照点/时间旅行/多写者同步——DSH 的 sync/replay
  全套不引入，单写者单机足够）。
- Msg 模型本身不变（仍是 LLM 上下文与 `/messages` API 的返回类型）。

## 2. 需求范围

### 2.1 功能需求

- [x] **FR1 SessionLog**（`packages/ftre-agent/src/ftre_agent/session/log.py`）：
      `append(type, data, message_id=None) -> SessionEvent` 同步纯内存提交——
      ① JSON 纯净单遍校验（拒绝非 JSON 值）② 深拷贝冻结（防 append 后篡改）
      ③ `seq = len(log)` 严格连续 ④ 重入禁止（append 回调内再 append 抛错）
      ⑤ 提交后 fire-and-forget 通知订阅者（观察者异常隔离，不影响提交）
      ⑥ 热路径零 I/O。
- [x] **FR2 load（恢复入口）**：`SessionLog.load(events, *, validate=True)` 从
      持久化层读入的序列重建内存日志；校验 seq 连续（index i 必须 seq=i）；
      未知事件类型按 `ignorable` 标记决定跳过或拒绝（F41 FR8）。
- [x] **FR3 write-behind 持久化**（`src/ftre/services/session/persistence/jsonl.py`）：
      独立组件订阅 append——① 事件深拷贝入批队列 ② 默认 **200ms** 批窗口 flush
      ③ 失败进入 backoff 并告警，保留批次直到成功或显式关闭（不阻塞 append）④ per-session 串行
      （写序 = 事件序）⑤ flush 时校验批次首事件 seq = 文件 cursor+1，错位即抛
      （写侧连续性契约）。
- [x] **FR4 文件格式与原子性**：`sessions/<sid>/session.jsonl`；首行
      `{"v":1,"format":"ftre-session-log"}`；每行是一个普通事件 JSON 或一个
      **无损 chunk storage row**；storage row 不是事件类型，读取时必须还原成原始
      `assistant/chunk` 序列；append 模式写 + flush；进程内首发（文件不存在）用
      tmp+fsync+os.replace 原子发布；写失败 truncate 回滚到旧 size（防重复 seq）；
      目录 fsync。
- [x] **FR5 checkpoint 边界**（由 `SessionService.flush_log()` 提供）：
      语义强制 flush 点——① LLM 请求前（Reasoning stream 入口）② 顶层工具执行前
      ③ 每 turn 结束后（turn/end append 时同步 flush）。Runtime 通过
      `runtime_context["log_flush"]` 注入该窄方法，不引入独立策略模块；flush 失败不阻断业务
      （降级为告警 + 下一次窗口 flush 兜底）。
- [x] **FR6 UserMsg 存储时序**（v3 FR6 契约在新架构下重述）：admission 仅 Inbox
      排队项（不动）；**claim 时先 `append(user/message)` 后 `repository.claim`
      后 queue 快照**——F41 I1 不变量在 SessionLog 下的实现；幂等：request_id
      已存在对应 `user/message` 事件时跳过（查会话内存索引，L1 操作）。
- [x] **FR7 repair（崩溃恢复策略）**：load 时对末尾不完整 turn 的处理——① 截断
      torn-tail（JSONL 解析失败的最后一行）② 为开着的 turn 合成关闭事件：
      `tool/result(state=interrupted)`（对未闭合 tool_call）+ `turn/end(outcome=
      cancelled, reason="crashed")`。合成事件在 `SessionLog.load` 前原子写回，
      作为事实日志持久化，避免每次重启重复 repair；标记 `metadata.synthetic=true`。
- [x] **FR8 deriveMessages（读侧 fold）**（`packages/ftre-agent/src/ftre_agent/
      session/derive.py`）：`derive_messages(events) -> list[Msg]` 幂等纯函数——
      按 surface 规则（F41 FR3）fold：`user/message`→UserMsg、`assistant/message`
      →whole-value AssistantMsg（替代其前 chunk 聚合）；在 whole-value 到达前，
      `assistant/chunk(kind=text|thinking|tool_result_text)` 按稳定块 id 折叠为
      in-flight Assistant，`tool/result-start` 创建 running ToolResultBlock；
      `tool/result`→配对
      toolCall 定稿、`hint/message`/`compact/message`→对应块；增量优化：
      `SessionLog.derive()` 记录已派生 seq 水位，只 fold 新增事件（DSH
      deriveMessages 同款）。**同时是** `/api/sessions/:id/messages` 与 LLM 上下文
      构建的唯一来源（替代 `get_context_messages` 的 state.json 读取）。
- [x] **FR9 compact 锚点**：`compact/message` 事件的 `through_message_id` +
      `derive_messages` 的锚点裁剪逻辑（最后 compact 之后）替代现有
      `get_context_messages` 的 through_id 扫描；fast 模式（裁剪工具输出）以
      `compact/message(metadata.mode=fast, tool_results=n, tool_result_ids=[…])` 表达，
      derive 优先按精确 id 跳过被裁剪 tool_result 的正文（保留占位）；旧日志没有
      id 时才按该 compact 事件之前的数量兼容裁剪。
- [x] **FR10 事件收敛映射实施**：Runtime 三执行器（reasoning/acting/exit）的
      `yield AgentStreamEvent` 改为 `session.append(新事件)`（映射表 F41 附录 A）；
      LLM 流式消费处保留局部 BlockAssembler；工具参数不再发 delta 事件（内部流式照旧，
      定稿才 append）。Assistant whole-value 的收口时机由 v5 FR16 统一规定。
- [x] **FR11 转发器与帧**：SessionLog 订阅 → `publish_frame(session/event)` 透传
      （双投沿用 F41 FR5）；`session/subscribed` 于 attach 时发送；
      tail-page HTTP `GET /api/sessions/:id/events?after_seq=&limit=` 读内存日志
      （含未 flush 事件——内存即权威）。
- [x] **FR12 双写退场收口**：实现不再读取或声明 `session_log_dual` 配置；
      SessionLog 是唯一会话事实源，旧投影/双写路径按 §3.7 物理删除。
- [x] **FR13 Inbox/Compaction 接口适配**：Inbox `_persist_user_messages` 改调
      SessionLog append（FR6）；CompactionService 摘要写入改 append
      `compact/message`；`emit_maintenance` 的 start/failed 保持非日志（瞬时态）。

### 2.2 非功能需求

- append 吞吐 ≥ 10k 事件/s（纯内存 dict/list 操作，远超 LLM 流速）。
- write-behind 丢批上界 = 200ms 窗口 + 未触发的语义 flush 间隔（`flush_log()`
  把崩溃损失框在"当前 step"内）。
- 内存：SessionLog 事件常驻（单会话消息量级 <100k 事件，可接受；超大会话的日志
  换页留作未来优化，不在本期）。
- **体验基线**：desktop 全部既有交互零回退（发送/流式/取消/确认/压缩/队列/重连/
  历史），断线 chunk 级精确恢复与跨 channel 实时观察为纯新增增益——"体验只会变好"
  是本阶段交付承诺。

### 2.3 补充功能需求（交付形态）

- [x] **FR14 冷启动交付形态**：实施完成即终态——用户删除旧数据
      （`~/.ftre/sessions/` 全部 state.json 会话目录）后启动 `ftre gateway`，
      系统即以纯 SessionLog 架构运行：无旧代码路径可被触发（P3 已物理删除），
      无迁移/双读逻辑存在（本来就不写），新会话全部使用 `session.jsonl`；
      运行时不提供旧投影或双写开关。

- [x] **FR15 chunk storage codec（v5）**（`src/ftre/services/session/persistence/`）：
      连续且同一 `message_id/kind/block_id/tool_call_id` 的 `assistant/chunk` 增量，
      在写盘批次内编码为一个 storage row；row 保存首 seq/time、时间间隔和每个
      delta，读取时还原出与原事件逐字段一致的事件。无法完全识别的字段、未来 chunk
      变体或不足 3 条的序列原样写入，禁止静默丢数据。该编码只属于物理存储层，不能
      出现在 `ALL_EVENT_TYPES`、SessionLog 或 WebSocket wire 协议中。

- [x] **FR16 Assistant 语义边界收口（v5）**：ReasoningExecutor 继续发布
      `assistant/chunk` 作为实时事件，但不再在每次 LLM 调用后写同一条累计
      `assistant/message`。当前 Turn 的 Assistant 内容只在 completed、paused、error
      或 cancelled 等语义边界写入一次；内存 MessageContext 仍完整保留跨工具步骤内容。
      TurnExecutor 在 `turn/end` 前负责唯一的最终 Assistant 快照；空内容不创建快照。

- [x] **FR17 Token 作用域分离（v5）**：
      `assistant/message.token.usage` 表示当前 Turn 的累计用量，
      `assistant/message.token.last_call_usage` 表示最后一次成功 LLM 调用用量；
      `turn/end.data.usage` 只表示 Turn 累计用量，处理 `turn/end` 时不得覆盖
      `last_call_usage`。Runtime 在每次成功调用时更新最近调用快照，在语义边界构造
      最终 Assistant 时同时写入两个作用域。

- [x] **FR18 压缩水位使用当前上下文（v5）**：`SessionService`/`CompactionService`
      的自动压缩判断使用 `last_call_usage.prompt_tokens + pending_estimated`（网关缺失
      prompt_tokens 时使用 `total_tokens - completion_tokens`），再扣除 `max_output`
      与 safety buffer；`turn_usage` 仅用于统计、审计和压缩结果元数据，不得作为当前
      请求上下文水位。

- [x] **FR19 恢复与落盘幂等修复**（本轮回归）：末行 JSON 损坏必须物理截断；repair
      合成的 `tool/result`/`turn/end` 必须在 `SessionLog.load` 前原子写回，不能每次重启
      重复生成；repair 只能收尾当前未闭合 turn，禁止把历史 turn 的 assistant 作为目标。

- [x] **FR20 write-behind 失败语义**：失败批次保留在队首按 seq 重试，后续事件不得越过；
      `flush()` 只有在批次实际落盘后才成功，失败必须将异常返回调用方，禁止“未落盘但
      barrier 成功”。

- [x] **FR21 derive 确定性与压缩边界**：user block 缺失 id/时间时从事件坐标稳定派生，
      非 LLM UI part 不得使历史 fold 失败；summary compact 按 `through_message_id`
      保留压缩期间新增消息；fast compact 持久化精确 `tool_result_ids`，禁止旧 compact
      的数量误裁未来输出。

- [x] **FR22 Turn/客户端一致性**：一个 Turn 可产生多个 assistant message_id 时，每个
      新增/变更消息只发布一次最终快照；工具结果元数据在内存与 whole-value 快照中一致，
      `tool/result-start` 先于工具输出；客户端历史与直播共用同一 Msg 投影，turn/end
      累计 usage 不得覆盖 `last_call_usage`，并展示 `context_tokens`。

- [x] **FR23 历史快照与游标原子性（本轮回归）**：`SessionService` 读取一次
      `SessionLog.events`，在这份不可变事件列表上完成 derive、分页和 `last_seq` 计算；
      `/api/sessions/:id/messages` 不得先读消息再单独读取游标。快照可以返回尚未结束的
      Assistant chunk，后续 whole-value 仍按同 `message_id` 替换。

## 3. 技术方案

### 3.1 模块布局

```text
packages/ftre-agent/src/ftre_agent/session/     # 新增：纯逻辑层（无 IO、无 Host 依赖）
  events.py        # 13 事件 Pydantic 模型 + 信封 + surface/ignorable 标记（F41 §4.2 实现）
  log.py           # SessionLog（FR1/FR2）
  derive.py        # derive_messages 纯函数（FR8）
src/ftre/services/session/
  service.py       # 装配 + 对外窄方法（append/get_events/derive 快照）
  persistence/jsonl.py     # write-behind + 文件原子性（FR3/FR4）
  persistence/chunk_rows.py # assistant/chunk 物理编码/解码（FR15）
  repair.py                # torn-tail + 合成关闭事件（FR7）
  router.py                # +/events tail-page 端点（FR11）
```

### 3.2 数据流

```text
Runtime executor ──append──► SessionLog（校验/冻结/seq）──notify──►
   ├─ write-behind（200ms 批）──pack──flush──► session.jsonl（原子追加）
   ├─ 转发器 ──► session/event 帧 ──► ChannelManager 双投
   └─ derive 水位缓存 ──► /messages API、LLM 上下文构建、compact 锚点
崩溃重启：load(JSONL) → repair（torn-tail 截断 + 合成 turn/end）→ 会话恢复
```

### 3.3 事件收敛映射（Runtime 改造落点）

| Runtime 现文件 | 改造 |
|---|---|
| `executors/reasoning.py` | chunk 事件改 `assistant/chunk`；只在内存组装响应，不在每个 step 写累计 whole-value；MODEL_CALL_* 删除（usage 并入）；RETRY → `turn/retry` |
| `executors/acting.py` | TOOL_CALL_* → `tool/call-start`（whole-value）；TOOL_RESULT_* → `tool/result-start`/`assistant/chunk(kind=tool_result_text)`/`tool/result`；REQUIRE_USER_CONFIRM → `approval/asked`；denied 三元组 → `tool/result(state=denied)` |
| `executors/exit.py` | ReplyEnd → `turn/end`（outcome/reason/error/usage/iterations） |
| `react_runner.py` | ReplyStart → `turn/start` + 首个 message_id；恢复 prologue 事件同映射 |
| `turn_executor.py` | PIPELINE_*/TURN_* `_emit_step` 全部删除；`turn/start(trigger=…)` 在 execute 入口、`turn/end` 在收尾（含 paused 分支） |
| `engine.py` | `_persist_inbound_user_message` → SessionLog append `user/message`；`_publish_session_status_async` 仅保留 blocked（`session/status` 事件） |

v5 对上表的修订：ReasoningExecutor 的 chunk 仍走实时事件流，但 Assistant whole-value
快照移动到 TurnExecutor 的语义边界；WriteBehindCoordinator 只在物理写盘时调用
chunk codec，SessionLog 内存事件与下行 wire 不变。

### 3.4 seq 与写序一致性

append 在事件循环单线程内执行（Runtime 逐事件驱动），天然串行；write-behind 每
session 一条写协程（asyncio.Queue），flush 前 cursor 校验保证文件序 = 事件序；
双投转发在 notify 回调内同步构造帧（dump 一次，双投复用同一字符串，I7）。

### 3.5 repair 细节

load 流程：①逐行 JSON 解析，末行失败 → 物理截断 + 告警 ②seq 校验（FR2）③扫描开着的
turn（有 `turn/start` 无 `turn/end`）与未闭合 tool_call → 生成合成关闭事件（标记
`synthetic: true` 于 data.metadata）④原子写回 session.jsonl ⑤写入 derive 水位。

### 3.6 UserMsg 幂等（F35 §13 语义重述）

会话内存索引 `request_id → message_id`（由 `user/message` 事件构建）；claim 时查
索引命中即跳过 append。fingerprint 冲突检测（同 request_id 不同内容）保留——
命中但 content 不一致时抛错（复用 v3 的 request_fingerprint 逻辑，挪到 SessionLog
append 的 user/message 前置检查）。

### 3.7 退场清单（P3 删除）

```text
src/ftre/services/session/projection.py          # SessionProjection 整体
packages/ftre-agent/src/ftre_agent/message/_msg.py::append_event   # 折叠方法
packages/ftre-agent/src/ftre_agent/event/_event.py                  # 日志事件类/EventType 退场；保留 Runtime 输入侧事件
src/ftre/services/session/events.py              # SessionEventService（被 SessionLog+转发器替代）
packages/ftre-agent-runtime/.../engine.py::_stream_queues / stream_input 适配  # task 工具改读 SessionLog
src/ftre/services/session/persistence/{repository,json_store} 的 messages 写路径  # state.json 消亡
MessageType: agent_event / session_event / session_event:command_message / global_event 等  # 收缩至 user_message/turn_cancel/downstream_frame
octo_plugin/_channel.py            # 改读事件（text chunk 累积 + assistant/message 收口 + turn/end 兜底），顺带修 CUSTOM bug
```

## 4. 接口定义

契约以 F41 为准；v5 不新增业务 Service 或 wire 事件，只新增持久化层内部编码函数：

```python
class SessionService:
    def log(self, session_id) -> SessionLog            # 唯一写入入口（Runtime 经注入调用）
    async def get_events(self, session_id, *, after_seq: int, limit: int) -> Page
    def derived_messages(self, session_id) -> list[Msg]  # derive 缓存快照
    async def get_messages_snapshot(self, session_id, *, limit_turns=None, before_ts=None)
        -> tuple[list[MessageModel], bool, int]  # messages/has_more/snapshot_last_seq
    # 既有 get_session/list/fork 保留；fork = 事件日志前缀拷贝（新实现）
```

存储层内部接口（不对 Agent/客户端暴露）：

```python
def pack_chunk_runs(events: Sequence[dict]) -> list[dict]: ...
def decode_storage_record(record: dict) -> list[dict]: ...
```

`pack_chunk_runs` 必须满足 `decode_storage_record(pack_chunk_runs(events)) == events`
（按事件顺序逐字段相等）；cursor 仍按原始事件数量前进，不能按物理 row 数量前进。

## 5. 验收标准

- [x] AC1：SessionLog 单测——seq 连续/重入禁止/JSON 纯净拒绝/冻结防篡改/观察者
      异常隔离，各一用例。
- [x] AC2：write-behind——1000 事件灌入，文件行序 = 事件序、cursor 校验触发
      （人为错位注入断言抛错）、200ms 批合并（flush 次数 < 事件数）。
- [x] AC3：崩溃恢复——kill -9 模拟（批窗口内）：重启后 torn-tail 截断、合成
      `turn/end(cancelled)` 存在、derive_messages 输出合法（无悬空 tool_call）。
- [x] AC4：`SessionService.flush_log()`——LLM 请求前 flush 被调用（Hook 桩断言）；
      flush 失败降级不阻断。
- [x] AC5：deriveMessages golden——共享 fixture 同源（`tests/fixtures/
      session_events_golden.json`）：服务端 `test_golden_fixture.py` 锁定 derive 输出 ==
      fixture.expected_messages（含确定性断言），F42 assembler 对拍同 fixture 逐字段
      一致（见 F42 AC1）。
- [x] AC6：FR6 时序——`user/message` 事件 seq < claim 后 queue 快照 seq（双路径：
      worker 与 steering）。
- [x] AC7：tail-page——after_seq 分页正确、含未 flush 内存事件、跳号补齐端到端。
- [x] AC8：双投 byte-identical（octo + ws 双桩）。
- [x] AC9：全量 `python -m pytest -q` 与 `python -m ruff check src packages` 通过
      （新基线，旧基线 209/45 中的投影/事件相关用例随退场清单同步移除/重写）。
- [x] AC10：`services/session/projection.py` 与 `SessionEventService` 不存在；
      `_msg.py` 无 append_event 定义；`grep -rn "IMMEDIATE_CHECKPOINT" src packages`
      为空；`SessionService.append_event` 是 v4 事件提交 API（合法存在）；旧
      state.json 目录存在时启动正常（忽略）。（原 grep 口径与新 API 同名，按语义
      修订，见变更记录）
- [x] AC11（冷启动 e2e，交付门禁）：模拟用户删除——将 `~/.ftre/sessions/` 全部旧
      目录移走后启动 `ftre gateway`：①启动日志无 ERROR/无投影路径调用 ②desktop
      连接正常 ③新建会话走 S1-S8 全场景（发送/流式/工具/确认 HITL/压缩/排队/
      steering/断线重连 tail-page/跨 channel 观察）逐项通过 ④重启 Gateway 后
      会话历史从 session.jsonl 完整恢复（消息、工具卡片、确认态一致）
      ⑤octo 桥接一条消息正常往返。本条是"体验只会变好"承诺的最终验收。

- [x] **AC12（v5）Assistant 快照数量**：模拟 26 次 ReAct LLM 调用、4679 个 chunk，
      一个完整 Turn 的事件中最多有一个最终 `assistant/message`（暂停/错误边界按
      实际边界各一次），不存在每步累计 whole-value 快照。
- [x] **AC13（v5）storage codec 无损**：写入至少 1000 个连续 chunk 后，物理 JSONL
      行数明显少于原始事件数；`read_event_log` 解码后 seq、time、message_id、data
      与原事件逐项相等；未知 chunk 形状保持原样；损坏 storage row 明确报错。
- [x] **AC14（v5）Token 作用域**：构造最后一次调用 42,977、整轮累计 779,247 的
      Turn，派生结果同时保留两者；`turn/end` 不覆盖 `last_call_usage`。
- [x] **AC15（v5）压缩判断**：模型上下文 1,000,000、max output 131,072、最近
      prompt 约 42,000 时不触发压缩；真实当前 prompt 达到阈值时触发；累计 Turn
      usage 不能单独触发压缩。
- [x] **AC16（v5）恢复兼容**：旧的逐事件 JSONL 与新的 packed JSONL 都能被同一
      `read_event_log` 读取，`SessionLog.load` 看到的仍是连续原始事件。
- [x] **AC17（本轮回归）刷新中的消息**：只写入 chunk、尚未写入
      `assistant/message` 的活动 Turn，通过 `get_messages_snapshot()` 仍能返回合并后的
      text/thinking/tool-result 内容；返回的 `last_seq` 与这份消息快照覆盖同一事件末端。

## 5.1 v5 实施顺序

1. 增加 `RunState.last_call_usage` 与最终 Assistant builder；停止 Reasoning 的
   重复 whole-value 事件。
2. 修正 `derive.py` 的 `turn/end` fold 和 `SessionService`/`CompactionService`
   的上下文水位字段。
3. 增加 storage codec，接入 JSONL 读写；最后补充回归与旧日志兼容测试。
4. 修复历史读取：derive 折叠 in-flight chunk，并让 `/messages` 的消息与游标来自同一事件快照。

不引入新的 Event 类型、Projection、Coordinator 或 Service；客户端沿用现有 wire，
同步服务端的 chunk fold 规则。

## 6. 测试计划

- 单元：SessionLog/derive/SessionService.flush_log/repair 全行为参数化。
- 契约：事件表 golden + fold 对拍（与 F42 共享 fixture，CI 双仓门禁）。
- 集成：Inbox→SessionLog→帧→桩 全链路；崩溃恢复（进程级 kill）；双写对拍期
  （新旧输出 diff 断言，P1/P2 专用）。
- 手动：S1-S8 e2e；重连 tail-page；压缩全模式；octo 桥接。

## 7. 迁移计划

| 阶段 | 内容 |
|---|---|
| F43-P1 | events.py/log.py/derive/持久化/flush 边界/repair 落地 + FR10 Runtime 收敛改造 + golden 对拍 |
| F43-P2 | tail-page + subscribed + desktop 切换（随 F42-P2）；octo 迁移 |
| F43-P3 | 退场清单执行（§3.7）+ 架构守卫 + MessageType 收缩 |

依赖：F41 approved 先行；F43-P1 与 F42-P1 可并行。

## 8. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-09-04 | v3 初稿（投影拆分方案） | — |
| 2026-09-04 | **v4 全量重写**：SessionLog 基础组合替代投影系全部组件；无旧数据兼容 | 用户决策采纳 DSH 事件日志架构；v3 的 FoldOutcome/ProjectionOutcome/ActiveReplies/reply_persistence 全部不再需要 |
| 2026-09-04 | 实施验收收尾：①事件计数修正为 13（对齐 F41）；②AC10 grep 口径按语义修订（SessionService.append_event 是新 API，与退役的 Msg.append_event 同名）；③AC8 双投补充 `tests/contracts/test_wire_frames.py::test_downstream_frame_dual_delivery_is_byte_identical` 证据；④FR5 checkpoint 三语义点（LLM 请求前/工具执行前/turn 结束）以内联 runtime_context 注入实现而非独立 checkpoint_policy.py 模块（turn/end 后同步 flush 已补齐）；⑤FR12 双写以 golden contract 测试 + 冷启动 e2e 替代（用户指令：一次性终态交付），`session_log_dual` 开关已物理移除（全仓零引用）；⑥AC5 跨语言对拍同 F42 AC1 列后续项 | 实施偏差如实记录；AC5 依赖共享 fixture CI 机制，超出一次性交付形态 |
| 2026-09-04 | 收尾补齐：实施 AC5 共享 fixture 对拍（`tests/fixtures/session_events_golden.json` + `test_golden_fixture.py` 回放/覆盖面/确定性三断言）；derive fold 补确定性 parity——hint 块 id=`hint_{seq}`、compact 块 id=`compact_{message_id}`、tool_call created_at/finished_at 取事件时间、compact fast 消息补 finished 终态 | 跨语言对拍要求两侧 fold 无随机生成；776 tests 全绿 |
| 2026-09-04 | v5 增量修订：增加 chunk storage codec、Assistant 语义边界收口、Token 作用域分离和当前上下文压缩判断；保持 F41 wire 与 SessionLog 内存事件不变，修复生产会话文件膨胀和 1M 上下文误触发压缩问题 | 依据生产会话 `ws_sess_13d6b603a1c3` 的 4814 事件分析 |
| 2026-09-04 | v5 实施验收：`chunk_rows.py` 接入 JSONL 原子写/读取；TurnExecutor 在 `turn/end` 前唯一收口 Assistant；derive 保留 `last_call_usage`；压缩使用 `context_tokens`；新增回归测试；全量 pytest 786 passed、Ruff 和 diff check 通过 | 修复会话文件膨胀与 1M 上下文误压缩 |
| 2026-09-05 | 缺陷修复回归：末行物理截断与 repair 原子落盘/当前 turn 绑定；write-behind 失败批次保序重试且 flush 不再假成功；user fold 的 block id/时间确定性；summary cutoff、fast `tool_result_ids`；Turn 多 assistant 快照、工具结果 metadata/order；客户端统一 Msg 投影、turn/end token scope 与 `context_tokens` | 真实 session 审计发现重复恢复、旧 fast compact 误裁未来输出、消息时间/id漂移、工具元数据丢失和客户端累计 token 误显示 |
| 2026-09-06 | 收尾审计：PRD 模块树与事件数量统一为 13，移除不存在的 `checkpoint_policy.py`/运行时双写描述；`SessionService.fork_session()` 统一经 `log()` 入口后再复制，未加载父会话也会先 repair；删除会话和关闭服务时清理 per-session 装配锁；新增未加载父会话 fork repair 回归测试，后端 794 tests、桌面 592 tests、Ruff、TypeScript 与 diff check 通过 | 消除文档与实现漂移，避免 fork 复制悬空 turn，并收口生命周期缓存 |
| 2026-09-06 | 刷新期间流式消息修复：derive 折叠 text/thinking/tool_result_text chunk；新增 `get_messages_snapshot()`，保证 `/messages` 的消息和 `last_seq` 同源；客户端同步回退注释与缺失 block_id 规则 | 原实现只在直播客户端聚合 chunk，服务端历史只 materialize tool call；独立读取消息与游标会在并发追加时跳过 chunk |

## 9. 附录 B：目标文件夹结构（三仓联动终态）

`★` 新增 / `✎` 重写 / `✂` 删除 / `＝` 不变。

### B.1 ftre 后端（E:\ftre）

```text
src/ftre/
├─ services/session/
│  ├─ service.py                      ✎  装配 SessionLog；对外窄方法 append/get_events/
│  │                                     derived_messages；fork=日志前缀拷贝
│  ├─ projection.py                   ✂  SessionProjection 整体退场
│  ├─ events.py                       ✂  SessionEventService（被 SessionLog+转发器替代）
│  ├─ repair.py                       ★  torn-tail 截断 + 合成关闭事件（FR7）
│  ├─ router.py                       ✎  + GET /api/sessions/:id/events（tail-page）
│  ├─ persistence/
│  │  ├─ jsonl.py                     ★  write-behind（200ms 批）+ 原子写（FR3/FR4）
│  │  ├─ chunk_rows.py                ★  assistant/chunk 无损物理编码（FR15）
│  │  ├─ repository.py                ✎  删 messages 写路径；保留 session CRUD/索引
│  │  └─ json_store.py                ✎  会话元信息保留；messages 结构搁置
│  ├─ message/{converter,multimodal}.py   ✎/＝  converter 输入改 derive 结果
│  └─ entity/{models,state}.py        ＝/✂  models 保留；state 的 messages 退场
│
packages/ftre-agent/src/ftre_agent/
├─ session/                           ★  协议心脏（纯逻辑，无 IO/无 Host 依赖）
│  ├─ events.py                       ★  13 事件模型 + 信封 + surface/ignorable（F41 §4.2 唯一实现）
│  ├─ log.py                          ★  SessionLog：append/load/订阅（FR1/FR2）
│  └─ derive.py                       ★  derive_messages 纯函数（FR8）
├─ message/_msg.py                    ✎  保留 Msg/Block/工厂；✂ append_event（250 行折叠）
├─ event/_event.py                    ✎  ✂ 旧日志事件类 + EventType；保留 EventBase、
│                                        HintBlockEvent、UserConfirmResultEvent（Runtime 输入协议）
│
packages/ftre-agent-runtime/src/ftre_agent_runtime/
├─ executors/{reasoning,acting,exit}.py   ✎  yield 旧事件 → session.append 新事件（§3.3 映射）
├─ react_runner.py                    ✎  ReplyStart → turn/start + message_id
├─ turn_executor.py                   ✎  ✂ _emit_step 五处；turn/start(trigger) 入口、
│                                        turn/end 收尾（含 paused）
├─ engine.py                          ✎  user message → append；✂ _stream_queues；
│                                        status 收缩为 blocked
│
src/ftre/services/messaging/
├─ wire.py                            ★  6 帧模型 + DownstreamFrame 联合（F41 §4.4）
├─ bus/{service,protocol,bus}.py      ✎  publish_frame；MessageType 收缩至
│                                        user_message/turn_cancel/downstream_frame
├─ channel/manager.py                 ✎  downstream_frame 双投；✂ MIRROR_TO_WS_CHANNELS
│
src/ftre/plugins/builtin/
├─ channels/websocket/channel.py      ✎  透传 send + attach 基线三连 + rpc 合并直回；
│  │                                  ✂  _send_reply_snapshot
├─ command/plugin.py                  ✎  command_message → session.maintenance 帧
├─ {session_title,plan,schedule}/     ✎  产出改走 session/projection 帧（注册表）
│
scripts/gen_wire_types.py             ★  Pydantic → JSON Schema → wire.gen.ts
tests/contracts/test_{session_events,wire_frames}.py   ★  golden（F42 对拍共享）
```

### B.2 运行时数据目录（~/.ftre）

```text
~/.ftre/sessions/<sid>/
├─ state.json                         ✂  旧格式（用户删除，视为不存在）
└─ session.jsonl                      ★  事件日志：首行 {"v":1,"format":"ftre-session-log"}，
                                        每行一事件或 packed chunk row，append-only，
                                        tmp+replace 原子首发（读取时还原 chunk）
```

### B.3 ftre-desktop（E:\binn\ftre-desktop\packages\renderer\src）

```text
types/wire.gen.ts                     ★  生成产物（禁手写）
services/websocket-client.ts          ✎  帧解析 + subscribed + rpc 结算；✂ 手写事件类型
stores/
  ├─ conversationAssembler.ts         ★  ConversationAssembler（fold 纯函数 ~200 行）
  ├─ sessionEventClient.ts            ★  seq 游标 + tail-page 补齐 + 直播缓冲合并
  ├─ chatProjection.ts                ✎  applyEvent 分发；✂ applyFrame/applyEvent 旧实现/
  │                                     BusEvent/seenEventIds/client_connection_epoch
  └─ chat.ts                          ✎  帧路由简化；✂ isCoreEvent 正则
features/chat/**                      ＝  UI 组件零改动（ChatInput 数据源换、组件不变）
```

### B.4 外部（C:\Users\蒋全明\.ftre\plugins\octo_plugin）

```text
_channel.py                           ✎  改读事件（text chunk 累积 + assistant/message
                                         收口 + turn/end 兜底）；顺带修 CUSTOM bug
```

结构要点：①`ftre-agent/session/` 是协议心脏（词汇 + fold 规则唯一定义，服务端读侧
与客户端 golden 共指）；②投影系组件是**整路径消失**而非重构（§3.7 清单）；③持久化
由 `jsonl`/`repair` 两个物理模块与 `SessionService.flush_log()` 语义边界组成，分别可测；
④P1 净新增仅 `ftre-agent/session/` 三件 + jsonl + repair + wire + golden，可先行合入
不触现有链路。
