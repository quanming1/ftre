# PRD-F42 客户端会话状态机与 UI 投影（v4，事件 fold 消费端）

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收
> **本版为 v4 重写**：输入源从"v3 的 10 帧"改为"F41 的 6 帧 + 13 事件表"；
> 状态机/按钮/五阶段等 UI 契约自 v3 原样继承，仅替换消费引擎。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F42 |
| 名称 | 客户端会话状态机与 UI 投影（事件 fold 消费端） |
| 状态 | 开发中（缺陷修复回归） |
| 创建日期 | 2026-09-04 |
| 定稿日期 | 2026-09-04 |
| 验收日期 | 2026-09-04 |
| 关联文档 | `docs/prd/PRD-F41-downstream-wire-protocol.md`（**唯一依赖**，事件表/帧表/恢复协议以 F41 为准）、`docs/prd/PRD-F43-server-event-pipeline.md`；实施仓库 `E:\binn\ftre-desktop` |

## 1. 背景与目标

### 1.1 v4 客户端职责变化

v3 客户端消费"服务端投影好的快照"（reconcile）；v4 事实源是事件日志，客户端需要：
①实现 **ConversationAssembler**——把 `session/event` 流 fold 成消息列表的幂等纯函数
引擎（DSH conversation-assembler.ts 同构）；②实现**恢复客户端**——subscribed/tail-page
补齐逻辑。换来的是：断线精确恢复（chunk 级）、消息列表可从任意 seq 重建、
服务端零投影负担。

UI 层完全不变：ChatMessage DTO、全部组件、按钮状态机、五阶段生命周期、队列横幅规则
自 v3 §3.2-3.4 **原文继承**（本 PRD 直接引用，不重写）。

### 1.2 目标

定义 desktop 的**事件消费引擎**（fold 规则 + seq 游标 + 恢复协议）与状态机输入适配，
保持"每个 UI 状态迁移可查表、无多源投票"的 v3 承诺。

### 1.3 非目标

- 不改 UI 组件与 ChatMessage DTO（与 v3 相同）。
- 不做多端 fold 一致性协议（各自 fold + golden 测试保证等价）。

## 2. 需求范围

### 2.1 功能需求

- [x] **FR1 ConversationAssembler**：`fold(state, event) → state'` 幂等纯函数引擎；
      规则表 = F41 §4.2 事件表的 fold 语义列（§3.1 展开）；输入 `event.seq ≤
      state.lastSeq` 时直接跳过（幂等重放安全）。
- [x] **FR2 seq 游标与恢复**：维护 per-session `lastSeq`；收到 `session/subscribed`
      时若 `local.lastSeq < last_seq` 或直播帧跳号 → 请求 tail-page
      `GET /api/sessions/:id/events?after_seq=` 重放补齐（分页循环至追平）；
      补齐期间直播帧入缓冲，追平后按 seq 合并消费。
- [x] **FR3 会话状态机**：迁移表自 v3 §3.1 继承，驱动事件替换为——
      `turn/start`→dispatching→executing 入口、`turn/end(outcome)`→idle/error/
      cancelled/paused、`session/status(blocked)`、`session/maintenance`（compaction）
      ——**一态一源**不变。
- [x] **FR4 UI 契约继承**：发送按钮状态机（含 Loading）、用户消息五阶段
      （P0 optimistic→P1 admitted→P2 dispatching→P3 active=`user/message` 事件→
      P4 done=`turn/end`）、队列横幅规则——三表自 v3 §3.2/§3.3/§3.4 原样生效，
      仅触发物改为对应事件（P3 由 `user/message` 事件驱动）。
- [x] **FR5 消息渲染**：assembler 产出的 Msg（六种 block + toolCall/toolResults
      配对）经既有 `msgToChatMessage` 投影为 ChatMessage；`assistant/message`
      whole-value 到达即整条替换（丢弃其前 chunk 的临时聚合）；`approval/asked`
      驱动确认卡（含 reason/rule_id）；`tool/result` state=denied/interrupted
      合成对应卡片态。
- [x] **FR6 projection/maintenance 帧**：`session/projection`（todo/plan/title/token
      快照值直接写 store，last-wins，无 fold）；`session/maintenance(command_message)`
      渲染指令气泡。
- [x] **FR7 outbox 与 rpc**：同 v3（request_id 幂等重发、`rpc` 帧结算、`session/queue`
      快照 revision 丢弃旧值）。
- [x] **FR8 未知事件忽略**（F41 FR6 的消费端落地）：assembler 对未知 type 跳过
      并计数（诊断日志），不影响游标推进。

### 2.2 非功能需求

- **性能**：chunk 批处理 10ms 合帧 flush 保持；assembler 输出的 ChatMessage 引用
  稳定性（memo 契约）为回归门禁——`assistant/message` 替换时只对该 message 构造新引用。
- **正确性红线**：text append-only 假设保持；fold 幂等性（同事件重放 N 次结果相同）
  是硬测试项。

## 3. 技术方案

### 3.1 Fold 规则表（与 F41 事件表一一对应）

| 事件 | assembler 动作 |
|---|---|
| `user/message` | 追加 UserMsg 气泡（metadata.hide 决定显隐；steering 注入场景识别） |
| `assistant/chunk(kind=text/thinking)` | 按 message_id+block_id 追加块（无块则开块） |
| `assistant/chunk(kind=tool_result_text)` | 按 tool_call_id 追加 toolResult.result |
| `tool/call-start` | 开立 toolCall block（arguments 已 whole-value） |
| `tool/result-start` | toolResult 置 running |
| `tool/result` | 定稿 toolResult（state/output/metadata）+ 配对 toolCall 置 finished |
| `assistant/message` | **整条替换**该 message_id 的聚合结果（token/finished 落定） |
| `hint/message` | HintBlock（默认隐藏渲染） |
| `compact/message` | compact 气泡（role=user, name=compact，锚点语义） |
| `approval/asked` | 确认卡 asking 态 |
| `turn/start` `turn/retry` `turn/end` `session/status` | 驱动状态机（§FR3），不产消息 |

### 3.2 模块结构

```text
stores/conversationAssembler.ts   # 新增：fold 引擎（纯函数，~200 行）
stores/sessionEventClient.ts      # 新增：seq 游标 + tail-page 恢复 + 直播缓冲合并
stores/chatProjection.ts          # 重写：applyEvent(state, event) 分发 → assembler/状态机；
                                   # 删除 v3 的 applyFrame/applyEvent 旧实现
services/websocket-client.ts      # 帧解析 + subscribed 处理 + rpc 结算（v3 结构沿用）
types/wire.gen.ts                 # F41 FR9 生成
```

完整目标结构见 PRD-F43 附录 B。

## 4. 接口定义

以 F41 为准（本 PRD 不重复 schema）；assembler 公开接口：

```ts
class ConversationAssembler {
  lastSeq = -1
  append(event: SessionEvent): void        // 幂等；seq ≤ lastSeq 跳过
  messages(): Msg[]                        // 当前聚合视图（快照引用稳定）
  activeAsking(): ApprovalCard[]           // approval 卡状态
}
```

## 5. 验收标准

- [x] AC1：fold golden——共享 fixture（`packages/ftre-agent/tests/fixtures/
      session_events_golden.json`，27 事件覆盖全表面事件 + approval→paused→confirm
      恢复 + compact summary/fast + error turn）回放，`conversationAssembler.golden.test.ts`
      断言 assembler 输出与服务端 `derive_messages()` 输出逐字段一致（归一化：剔除
      null/undefined、ISO 时间戳转 epoch ms——两侧运行时固有格式差异不构成语义差异）。
      为达成对拍，两侧 fold 补齐确定性 parity（hint 块 id 派生自事件 seq、compact 块 id
      派生自 message_id、tool_call created_at/finished_at 补事件时间戳）。
- [x] AC2：幂等性——同一段事件流重放 3 次输出引用级不变。
- [x] AC3：v3 全部 UI 验收项（AC2-AC10：五阶段/steering/HITL/断线/刷新一致性/
      跨 channel/性能）在事件输入下重跑通过。
- [x] AC4：**断线精确恢复**（v4 新增强项）——流式 chunk 中途断开 30s，重连后
      tail-page 补齐，消息文本与持续在线的第二客户端完全一致。
- [x] AC5：未知事件注入（future/x）不崩溃、游标推进、诊断计数 +1。
- [x] AC6：vitest 全绿（chat.test.ts 等测试改事件构造；UI 组件测试零改动）。

## 6. 测试计划

- 单元：assembler 全事件分支；seq 跳过/乱序；tail-page 分页循环。
- 对拍：与 ftre 仓 `deriveMessages` 共享 fixture 的 CI 双向断言。
- 集成：S1-S8 场景事件序列回放；双客户端一致性（AC4）。
- 手动：e2e 同 v3 清单 + 重连时 toast"正在同步历史…"体验。

## 7. 迁移计划

| 阶段 | 内容 |
|---|---|
| F42-P1 | wire.gen.ts + assembler + 恢复客户端并行实现（flag `useEventWire`）；旧链路保持 |
| F42-P2 | flag 切换灰度 → 全量；ChatInput 状态机换驱动源 |
| F42-P3 | 删除 v3 消费层（applyFrame/replySnapshotToChatMessage/旧帧类型） |

回退：flag 关闭即回（对应 F41-P1/P2 双写期）。

## 8. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-09-04 | v3 初稿（快照消费端） | — |
| 2026-09-04 | **v4 重写**：输入源改事件表 + assembler fold + tail-page 恢复 | F41 v4 方向反转；UI 契约继承 |
| 2026-09-06 | 审计修订：统一文档中的事件数量为 13，并记录当前跨仓回归状态 | F41 事件表实际为表面 5 + 流式 4 + 生命周期 4，避免客户端实现与 PRD 数量不一致 |
| 2026-09-04 | 实施验收收尾：FR1-FR8/AC2-AC6 全绿（590 tests + tsc）；AC1 的跨语言对拍落地为双侧独立 golden（服务端 `test_session_log.py::test_full_flow_golden` ↔ 客户端 `chat.test.ts` assembler 用例），共享 fixture 的 CI 双仓自动对拍未建立，与 wire codegen（F41 AC7）一并列后续项；TS wire 类型为手写 `types/wire.ts`（PRD §3.2 原文 wire.gen.ts） | 一次性终态交付（用户指令）以 golden contract 测试 + 冷启动 e2e 替代中间发布；跨仓 CI 门禁超出本交付形态 |
| 2026-09-04 | 收尾补齐：实施 AC1 共享 fixture 自动对拍 + wire 类型切换为生成产物（F41 AC7 联动）——`session_events_golden.json` 成为双侧 fold 的唯一事实源（desktop 侧经 gen 脚本同步为 `types/wire.golden.json`）；两侧 fold 补确定性 parity（hint/compact 块确定性 id、tool_call 终态时间戳）；desktop 消费层 import 切至 `@/types/wire.gen`，手写 wire.ts 删除 | 对拍从「双侧独立 golden」升级为「同一 fixture 双侧断言」；592 tests 全绿 |
| 2026-09-05 | 缺陷修复回归：历史加载移除重复的 persisted Msg 投影，统一复用 `msgToChatMessage`；user 附件映射与 skill part 保留；修复 turn/end 累计 usage 覆盖 `last_call_usage`；状态面板助手占比改用 assistant_messages；接入 context_tokens 水位 | 保证 HTTP 历史、WS 直播和 token UI 使用同一投影语义，避免刷新后显示与实时消息不一致 |
