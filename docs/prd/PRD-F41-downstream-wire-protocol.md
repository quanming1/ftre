# PRD-F41 会话事件日志协议契约（v4，历史档案）

> 本文是 F41 的历史设计记录，不再是当前持久化/恢复规范。持久化部分已由
> [PRD-F44](PRD-F44-session-snapshot-protocol.md) 的单文件 Msg Snapshot 替代；
> F41 的 Event 词汇和 `session/event` live 帧仍被 F44 复用，Snapshot 与跨进程
> reset 以 F44 为准。下文关于 JSONL 存储、无迁移和“事件日志唯一事实源”的内容只
> 用于解释当时的方案，不得作为新代码实现依据。

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收
> **本版为全量重写（v4）：放弃 v3 的"Msg 快照上 wire"路线，改为"事件日志即事实源"（DSH 模式）。
> 旧数据不兼容——存量 state.json 会话视为已删除，无迁移层。**

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F41 |
| 名称 | 会话事件日志协议契约（Session Event Log Wire Protocol v4） |
| 状态 | 已归档（wire Event 词汇由 F44 继续复用） |
| 创建日期 | 2026-09-04 |
| 定稿日期 | 2026-09-04 |
| 验收日期 | 2026-09-04 |
| 关联文档 | `docs/prd/PRD-F42-client-session-lifecycle.md`（消费端 fold）、`docs/prd/PRD-F43-server-event-pipeline.md`（生产端）、`docs/prd/PRD-F36-agent-core-consolidation.md` §7.2（旧事件词汇，由本 PRD 收敛替代） |

## 1. 背景与目标

### 1.1 v3 → v4 的方向反转（历史方案）

v3（本文件上一版）确定"Msg 快照为唯一下行事实"，仍保留**两个事实源**：进程内事件流 +
投影快照，投影类虽拆分但 `message.updated/delta` 双帧、revision 比较、快照兜底等复杂度
均源于"两源同步"。v4 采纳 DSH 验证过的模式：**事件日志是唯一事实源**——

- 流式输出 = 日志里的 `assistant/chunk` 事件（已提交即已持久化语义）；
- 完整消息 = 一个 whole-value `assistant/message` 事件；
- 会话可见历史 = 读侧幂等纯函数按需 fold（`deriveMessages`）；在 whole-value 快照到达前，
  也 materialize 当前正在生成的 Assistant；
- 恢复 = `session/subscribed{lastSeq}` 基线 + tail-page 事件重拉（**流式中途断线也能
  精确补齐**，优于 v3 的快照兜底）；
- 服务端**不存在**"正在生成回复"的内存聚合注册表（SessionProjection 及其继任者全部退场）。

**旧数据处置**：不迁移、不双读。存量 `sessions/<sid>/state.json` 视为已删除；Gateway 启动
时旧格式目录直接忽略（不报错、不转换）；新会话全部使用 `session.jsonl` 事件日志。

### 1.2 目标

定义三件契约，使任意消费端（desktop/octo/未来 SDK）仅凭本文档即可与 Gateway 完整交互：
①**事件表**（词汇 + payload schema + surface 规则）；②**透传帧**（6 种）；③**恢复协议**
（attach 基线 + tail-page + seq 语义）。

### 1.3 非目标

- 上行协议不变（`session.prompt` / `session.cancel` / `session.updateQueue` / `attach` /
  `detach`，F12 冻结）。
- 不做 global 级事件（session 列表变更仍走 HTTP）。
- 不做事件重放/时间旅行 UI（协议天然支持，但不在本阶段交付）。

## 2. 需求范围

### 2.1 功能需求

- [x] **FR1 事件信封**：所有会话事件共享
      `{type, seq, time, data, message_id?}`；`seq` 为会话内**从 0 严格连续**的整数
      （`seq = log.length`，DSH index.ts L629 契约）；`time` 毫秒时间戳；`data` 必须是
      JSON 纯净值（写入点单遍校验，拒绝 BigInt/函数/Date 等）。
- [x] **FR2 事件表（核心词汇，13 种）**：§4.2 全集即协议主体；每事件 payload schema
      在本文档唯一定义。旧 21 种 AgentStreamEvent 按附录 A 映射收敛，收敛后的类
      `AgentStreamEvent` 联合**对扩展关闭**——新能力一律新增事件类型（FR8）。
- [x] **FR3 surface 规则**：表面事件（`user/message`、`assistant/message`、
      `tool/result`、`hint/message`、`compact/message`）直接产生或替换 Msg；
      `assistant/chunk` 虽是非表面事件，但在 whole-value 快照到达前会按
      `message_id + block_id` 折叠为临时 Assistant 内容。生命周期事件不产生消息。
      fold 规则是幂等纯函数，服务端（deriveMessages）与客户端
      （ConversationAssembler）共享同一规则定义（各自实现，golden fixture 共享，见
      F42 §3.1）。
- [x] **FR4 透传帧（6 种）**：§4.4；`session/event` 帧**原样透传**事件（含完整信封），
      不包装、不改写、不附加渲染意图（DSH ToolEventView 的旁挂数据模式本阶段不引入）。
- [x] **FR5 双投路由**：下行帧投递 owner channel 与 ws 观察面（F42 跨 channel 实时
      观察）；`rpc` 帧单播直回。
- [x] **FR6 未知事件/帧忽略**：客户端收到未知 `type` 必须忽略不断连（跳过并计数）；
      日志恢复对未知类型默认拒绝重建，只有声明 `ignorable` 的事件才跳过（与 FR8
      一致，防静默丢数据）。
- [x] **FR7 seq 恢复协议**：attach → `session/subscribed{lastSeq}`；客户端本地
      `lastSeq < lastSeq` 或检测跳号 → 调 HTTP tail-page 重拉
      `GET /api/sessions/:id/events?after_seq=&limit=` 补齐后继续消费直播流。
- [x] **FR8 扩展机制（merge-extensible）**：新事件类型 = 在事件表声明
      `type + data schema + 是否 surface + ignorable`（默认必须拒绝重建，标 `ignorable`
      才允许读者跳过）——插件通过声明式注册扩展词汇，无需修改本协议正文；wire 上
      未知事件由 FR6 兜底。
- [x] **FR9 TS 类型同源**：`scripts/gen_wire_types.py` 导出事件表/帧表/Msg 镜像为
      desktop `types/wire.gen.ts`（手写 `types/wire.ts` 已删除，9 个 import 方切换）；
      构造侧可选性与开放字段经生成器内三张声明表固化，形状与类型全部来自 Pydantic。
- [x] **FR10 版本化**：log 文件头首行 `{"v":1,"format":"ftre-session-log"}`；`v=1`
      内只新增可选字段与新增事件类型，不改既有语义；破坏性变更 bump v 并拒绝旧文件。

### 2.2 非功能需求

- **性能**：`Session.append` 热路径零 I/O（纯内存提交）；透传帧零变换（dump 即发）。
- **正确性**：同 session 内 seq 严格连续（append 重入禁止 + 单线程）；日志文件内事件
  顺序 = 事件顺序（per-session 串行写，F43 §3.4）。
- **兼容性**：无旧数据兼容（见 1.1）；协议自身前向兼容（FR6/FR8/FR10）。

## 3. 技术方案

### 3.1 协议分层

```text
┌─ 进程内词汇（唯一事实源的写入侧）──────────────────────────────┐
│ SessionLog.append(type, data) —— 同步纯内存提交，seq=log.length   │
│   ├─► write-behind 持久化插件（批窗口+语义 flush，F43 §3.2）       │
│   │      → sessions/<sid>/session.jsonl（append-only）             │
│   ├─► 转发器 → session/event 帧（原样透传）→ ChannelManager 双投    │
│   └─► 派生读侧（非投影）:                                          │
│          deriveMessages() 懒 fold → LLM 上下文 / HTTP /messages     │
│          projection 注册表 → todo/plan/title/token → 帧（快照值）   │
└──────────────────────────────────────────────────────────────────┘
消费端（desktop/octo/SDK）：
  session/event 帧 + tail-page 重拉 → ConversationAssembler fold → UI
```

### 3.2 模块归属

| 契约 | 唯一定义处 | 消费方 |
|---|---|---|
| 事件表 + fold 规则 | `packages/ftre-agent/src/ftre_agent/session/`（新包内模块，纯类型 + 纯函数，无 IO） | F43 服务端 derive、F42 客户端 golden 对齐 |
| 帧表 | `src/ftre/services/messaging/wire.py` | Channel/publisher |
| tail-page HTTP | `src/ftre/services/session/router.py` 扩展 | 客户端恢复 |

完整目标文件夹结构（ftre / desktop / octo 三仓联动）见 PRD-F43 附录 B。

## 4. 接口定义

### 4.1 事件信封

```jsonc
{ "type": "assistant/chunk", "seq": 1042, "time": 1793474401234,
  "data": { ... }, "message_id": "m_9f01" }
// message_id：仅 surface 类事件与 chunk 事件携带（chunk 归属目标消息）；
// 生命周期类（turn/session 级）事件缺省。
```

### 4.2 事件表（13 种 = 表面 5 + 流式 4 + 生命周期 4）

#### 表面事件（构成消息，fold 输入）

| type | data | fold 语义 |
|---|---|---|
| `user/message` | `{content: Part[], metadata}` | 新 UserMsg（metadata 含 hide/request_id/agent_id） |
| `assistant/message` | `{message: Msg, usage?}` **whole-value** | 完整 AssistantMsg 替换同 message_id 的聚合临时形态 |
| `tool/result` | `{tool_call_id, name, output: Part[], state, metadata}` | 配对 tool_call 定稿（state: success/error/interrupted/denied） |
| `hint/message` | `{hint, source}` | HintBlock（hide，注入上下文） |
| `compact/message` | `{summary_text, through_message_id, trigger, tokens}` | compact 锚点 UserMsg（role=user, name=compact） |

#### 流式事件（非表面；chunk 的 message_id 指向聚合目标）

| type | data |
|---|---|
| `assistant/chunk` | `{kind: text\|thinking\|tool_input\|tool_result_text, block_id?, tool_call_id?, delta}`；text/thinking/tool_result_text 在最终快照前折叠进临时 Msg |
| `tool/call-start` | `{tool_call_id, name, arguments}` **whole-value**（不再流式拼参；见附录 A-7） |
| `tool/result-start` | `{tool_call_id, name}`；创建 running ToolResultBlock 占位，等待 chunk/result |
| `approval/asked` | `{tool_call_id, name, arguments, reason, rule_id}` |

#### 生命周期事件

| type | data |
|---|---|
| `turn/start` | `{turn_id, request_id, trigger: user\|command\|confirm\|cron\|plugin, command_name?, agent_id, model, queue_depth}` |
| `turn/retry` | `{turn_id, code, message, attempt, max_attempts}` |
| `turn/end` | `{turn_id, request_id, outcome: completed\|error\|cancelled\|paused, reason, error?, usage?, iterations, metadata?}` |
| `session/status` | `{status: blocked, reason}` **仅 blocked 突变**（executing 等由 turn 事件推导） |

### 4.3 事件时序不变量（I1-I7）

| # | 不变量 |
|---|---|
| I1 | `user/message` 先于 claim 后的 queue 快照（存储顺序契约，F43 FR6） |
| I2 | `turn/start` 先于该 turn 全部表面/chunk 事件；`turn/end` 后无该 turn 的 chunk |
| I3 | `assistant/message` 的 message_id 与其前置 chunk 序列一致；whole-value 到达前 fold 展示 chunk 累积内容，到达后以 whole-value 原子替换同一 message_id，避免重复正文 |
| I4 | `approval/asked` 后紧跟 `turn/end(outcome=paused)`；恢复由新 `turn/start(trigger=confirm)` 表达 |
| I5 | `tool/result` 与 `tool/call-start` 按 tool_call_id 配对；`turn/end` 前全部闭合（或由 repair 合成，F43 §3.5） |
| I6 | seq 严格连续：直播帧、tail-page、日志文件三处一致 |
| I7 | 双投两份帧 byte-identical |

### 4.4 帧表（6 种）

| 帧 | payload | 语义 |
|---|---|---|
| `session/event` | `{event: <完整事件信封>}` | 直播透传（零变换） |
| `session/subscribed` | `{last_seq}` | attach 基线锚点（客户端比对本地 lastSeq） |
| `session/queue` | 同 v3（F24 冻结形状） | 队列权威快照 last-wins |
| `session/projection` | `{key, value, seq}` | 派生状态快照（todo/plan/title/token，last-wins） |
| `session/maintenance` | `{name: command_message\|…, value}` | 指令反馈等非日志文本 |
| `rpc` | 同 v3（request_id/ok/value/error） | 上行结算（prompt→queue 快照；cancel→accepted） |

删除（vs v3）：`message.updated` / `message.delta` / `turn.started·retry·finished` 帧
（事件直接透传，不再翻译）/ `session.status` 帧的基线用途（改由 subscribed+status 事件
+ queue 基线三件套）。`confirm.requested` 帧 → `approval/asked` 事件。

### 4.5 恢复协议（FR7 展开）

```text
attach(sid) ─► session/subscribed{last_seq=N}   （在输出锁内，先于直播帧）
客户端: local.lastSeq < N 或后续帧跳号
        ─► GET /api/sessions/:sid/events?after_seq=local.lastSeq&limit=500
           返回 {events:[…], has_more}          （含流式中途的 chunk 事件）
        ─► fold 重放 → 追平后继续消费直播
历史加载: GET /api/sessions/:sid/messages 仍返回 fold 后的 Msg 数组（deriveMessages，
        形状不变，分页参数不变）；响应中的 `messages` 与 `last_seq` 来自同一事件
        快照，快照可包含尚未结束的 Assistant chunk——首屏仍走 Msg；增量/断线走事件补拉
```

## 5. 验收标准

- [x] AC1：事件表 golden 测试——13 事件各一 fixture（信封+data schema 校验），
      `tests/contracts/test_session_events.py` 全绿（事件表实数 13 种：表面 5 +
      流式 4 + 生命周期 4；原文 17 为分类计数笔误，见变更记录）。
- [x] AC2：seq 连续性——同 session 连发 1000 事件，直播帧/tail-page/JSONL 三处 seq
      严格连续且一致。
- [x] AC3：I1-I5 各一集成测试（真实 Inbox→SessionLog→帧 桩断言）。
- [x] AC4：断线精确恢复——流式 chunk 中途断开，重连后 tail-page 补齐至断点，
      客户端消息文本与服务端日志 fold 结果逐字节一致；刷新正在流式生成的会话时，
      `/messages` 直接返回当前 Assistant 文本，不得只返回 tool call。
- [x] AC5：双投 byte-identical（octo session 双桩验证）。
- [x] AC6：未知事件容错——客户端桩对 `{"type":"future/x"}` 跳过并计数不断连；
      日志恢复器对未声明 ignorable 的未知类型拒绝（FR6/FR8 口径）。
- [x] AC7：`gen_wire_types.py` 双跑产物 byte-identical（sha256 双跑一致；产物含
       `wire.gen.ts` 337 行 + golden fixture 同步拷贝 `wire.golden.json`）；CI 门禁
       以本地双跑 diff 为准（单机交付形态无 CI，见变更记录）。
- [x] AC8：旧 state.json 目录存在时 Gateway 正常启动（忽略不迁移），新会话全走 JSONL。

## 6. 测试计划

- 单元：事件 schema 校验；fold 纯函数（与 F42 共享 fixture）。
- 契约：golden 事件/帧 fixture；时序不变量断言。
- 集成：S1-S8 场景（v3 §4.5 骨架沿用，输入源换事件表）全链路桩测试；恢复协议
  （tail-page 分页/跳号/乱序）专项。
- 手动：desktop 全场景 e2e（F42 §6）；octo 桥接一条消息。

## 7. 迁移计划（无旧数据兼容 → 单向，仅版本双写期）

| 阶段 | 内容 |
|---|---|
| F41-P1 | SessionLog + 事件表 + wire 帧 + golden 测试落地；新会话即用日志（旧 state.json 会话直接不可见） |
| F41-P2 | desktop 切事件消费 + fold（F42）；octo 同步迁移 |
| F41-P3 | 删除 SessionProjection/append_event/state.json 写路径/旧帧（v3 全部残留）；架构守卫入库 |

回退：P1/P2 间为版本双写期（日志与旧投影并行）；P3 后不可回退（旧数据已视为删除）。

## 8. 附录 A：旧 AgentStreamEvent → 新事件表收敛映射

| 旧事件（21） | 新事件 | 说明 |
|---|---|---|
| REPLY_START / REPLY_END | 并入 `turn/start`（携带首个 message_id）与 `turn/end` | reply 概念由 turn+message_id 表达 |
| TEXT/THINKING_BLOCK_START·DELTA·END（6） | `assistant/chunk(kind=text/thinking, block_id, delta)` | START/END 由 chunk 流首尾自然表达；block 定稿靠 whole-value 覆盖 |
| TOOL_CALL_START·DELTA·END（3） | `tool/call-start`（whole-value arguments） | 工具参数不流式上 wire（LLM 内部流式仍在，落日志时只写定稿值） |
| TOOL_RESULT_START·TEXT_DELTA·END（3） | `tool/result-start` + `assistant/chunk(kind=tool_result_text)` + `tool/result`（whole-value） | 输出流式保留 chunk，定稿 whole-value |
| MODEL_CALL_START/END | 删除；usage 并入 `turn/end.usage` 与 `assistant/message.usage` | 每 turn 汇总 |
| REQUIRE_USER_CONFIRM | `approval/asked` | |
| USER_CONFIRM_RESULT | `user/message(content="/allow …", metadata.trigger=confirm)` + `turn/start(trigger=confirm)` | 确认回执本身是用户输入 |
| USER_MESSAGE | `user/message` | |
| RETRY | `turn/retry` | |
| HINT_BLOCK | `hint/message` | |
| （无对应）compact 三事件 done | `compact/message`（表面） | start/failed 仍是 `session/maintenance`（非日志，DSH 同款瞬态语义） |

## 9. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-09-04 | v3 初稿（快照协议） | 当时的外科手术方案 |
| 2026-09-04 | **v4 全量重写**：事件日志即事实源、透传帧 6 种、无旧数据兼容 | 用户决策采纳 DSH 模式；消除双事实源；存量数据视为删除 |
| 2026-09-04 | 实施验收收尾：事件表计数修正为 13（表面 5+流式 4+生命周期 4）；FR6 与 FR8 的未知类型策略统一为「默认拒绝、ignorable 才跳过」；AC5/AC6 按实现口径修订；新增 tests/contracts/test_{session_events,wire_frames}.py 作为事件/帧 golden 与双投 byte-identical 证据 | FR2 原文 17 为分类计数笔误；服务端 load 拒绝未知类型比静默跳过更安全（防数据损坏静默丢失），与 FR8 声明式注册扩展配套；AC1-AC8 中 AC7（gen_wire_types codegen）未实施——TS 类型当前为手写 `types/wire.ts`，codegen 列为后续项（用户已知） |
| 2026-09-04 | 收尾补齐：实施 FR9/AC7——`scripts/gen_wire_types.py` 落地（Pydantic → `wire.gen.ts`，双跑幂等 sha256 一致；手写 wire.ts 删除）；wire.py 帧 payload 类型化（subscribed/projection/maintenance/event 帧载荷契约模型 + RpcPayload/RpcError 契约模型，rpc 帧 payload 运行时保持 dict 直通以维持 value/error 缺省不上 wire 的既有字节形状）；golden fixture 同步纳入 gen 产物 | 消除 TS 手写副本漂移源；Pydantic 成为 wire 契约唯一事实源 |
| 2026-09-05 | 兼容字段修订：`compact/message` fast 模式增加可选 `tool_result_ids` 精确裁剪标识；`turn/end.data.metadata` 允许 repair 标记 synthetic；客户端仍须忽略未知字段 | 解决旧 compact 数量误裁后续工具输出，同时保持 v1 新增字段向前兼容 |
| 2026-09-06 | 修复刷新中的流式消息恢复：derive 在 whole-value 快照前折叠 text/thinking/tool_result_text chunk；`/messages` 以同一事件快照生成消息与 `last_seq` | 原实现只在客户端直播 fold chunk，服务端历史只生成 tool call，刷新后文本消失且新游标会跳过已读 chunk |
