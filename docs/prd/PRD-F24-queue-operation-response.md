# PRD-F24 Queue Operation Response 与 Inbox 快照统一协议

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F24 |
| 名称 | Queue Operation Response 与 Inbox 快照统一协议 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-25 |
| 定稿日期 | 2026-08-25 |
| 验收日期 | 2026-08-25 |
| 关联文档 | `docs/TODO.yaml` F24；F23 Steering 安全边界；`E:\binn\ftre-desktop\docs\prd\PRD-B5-queue-operation-response.md`；AGENTS.md |

## 1. 背景与目标

当前 WebSocket 队列操作存在两段响应：服务端先返回 RPC ACK，随后再通过
`session/queue` 推送最新队列。客户端必须维护 ACK、optimistic item、队列快照三套
短暂状态，导致 Steering 点击成功但界面仍显示旧 placement，普通消息还可能被错误标记为
“正在消费”。

本阶段把“操作结果”和“最新队列事实”合并为一个 Queue Operation Response。服务端在
Inbox 原子持久化完成后读取 wire snapshot，并在同一个响应中返回 `request_id`、`ok`、
`payload` 和服务端 `revision`。原始 `inbox.json` 继续是 Package 私有存储格式，不对外暴露。

### 非目标

- 不改变 Inbox 的 `next-turn`/`next-step` 内部语义和 claim 顺序。
- 不改变 Core Steering 在下一次 Reasoning 注入的语义。
- 不删除后台 claim 后向其他客户端推送 `session/queue` 的能力。
- 不为旧协议补兼容分支；缺少当前协议必需字段的旧帧直接拒绝或丢弃。

## 2. 需求范围

### 2.1 功能需求

- [x] **FR1：统一成功响应**：`session.prompt` 和 `session.updateQueue` 完成 durable mutation 后，返回 `type=session/queue`、原请求 `request_id`、`ok=true` 及完整 QueueSnapshot。
- [x] **FR2：快照必须包含 revision**：QueueSnapshot 增加单调递增的 Inbox revision；同一 Session 的响应和后台推送使用相同 revision。
- [x] **FR3：操作覆盖完整状态**：普通 queue 返回 `queued`，Steering 返回 `steering`，edit/remove 返回修改后的完整 items；客户端不读取 `next_turn`/`next_step`。
- [x] **FR4：错误保持统一 envelope**：失败仍返回 `request_id`、`ok=false`、`error.code/message/retryable`，不伪造成功快照。
- [x] **FR5：后台事实推送不变**：worker claim、插件 mutation、其他客户端 mutation 仍通过无操作 request_id 的 `session/queue` 主动推送。
- [x] **FR6：删除 WS 独立 Message ACK**：WebSocket 不再发送当前 `{ok:true,value:{accepted,...}}` admission ACK；ftre 不再提供 `getMessageAckPayload` 所需的独立协议。
- [x] **FR7：重试幂等**：相同 `request_id` 重试不会重复创建 QueueItem；成功重试返回同一队列事实，失败重试返回明确错误。
- [x] **FR8：协议边界**：原始 `inbox.json`、`next_turn`、`next_step`、Repository 字段不得出现在 WS payload。
- [x] **FR9：Steering 锁定**：进入 `steering`/`next-step` 交接区的用户消息在被 Core claim 前不可 edit/remove；操作返回明确错误，不产生成功快照。

### 2.2 非功能需求

- 响应必须在原子持久化完成后生成，不能返回内存未提交快照。
- snapshot 序列化失败必须返回可诊断错误，不能发送半截 payload。
- 连接输出仍使用当前 Session 输出锁，避免 operation response 与主动推送乱序。
- 不引入第二个 Queue Owner 或第二套 revision 计数器。

## 3. 技术方案

### 3.1 服务端 Owner

| Owner | 文件 | 职责 |
|---|---|---|
| Inbox Package | `packages/ftre-inbox/src/ftre_inbox/repository.py` | 持久化 revision，提供内部 snapshot |
| Inbox Package | `packages/ftre-inbox/src/ftre_inbox/service.py` | 将内部 snapshot 转成稳定 wire snapshot |
| WebSocket Channel | `src/ftre/plugins/builtin/channels/websocket/channel.py` | 将操作结果包装为 Queue Operation Response |
| WebSocket Plugin | `src/ftre/plugins/builtin/channels/websocket/plugin.py` | 继续监听 Inbox changed Hook，广播后台 queue snapshot |

`wire_snapshot()` 的公开结果只包含：`session_id`、`revision`、`items[].id`、
`items[].placement`、`items[].message.content/attachments`。

### 3.2 统一成功结构

```json
{
  "type": "session/queue",
  "request_id": "op-001",
  "ok": true,
  "payload": {
    "session_id": "ws_sess_1",
    "revision": 13,
    "items": []
  }
}
```

后台主动推送可以省略 `request_id` 和 `ok`，但必须携带相同 QueueSnapshot 结构。

## 4. 接口定义

### 4.1 `session.prompt`

请求协议不变。服务端在 Inbox admission commit 后返回 Queue Operation Response。

### 4.2 `session.updateQueue`

请求协议不变：`action=steer|edit|remove`。成功响应改为完整 QueueSnapshot，不再返回
`value.accepted/item_id`。

### 4.3 错误

```json
{
  "request_id": "op-001",
  "ok": false,
  "error": {
    "code": "item-not-pending",
    "message": "消息已不在队列中",
    "session_id": "ws_sess_1",
    "retryable": false
  }
}
```

## 5. 验收标准

- [x] **AC1**：普通 `session.prompt` 成功只返回一个带 queue payload 的成功响应，payload 含 revision 和 queued item。
- [x] **AC2**：`session.updateQueue steer` 成功只返回一个带 steering placement 的成功响应。
- [x] **AC3**：edit/remove 的响应快照分别反映内容修改和 item 删除。
- [x] **AC4**：后台 claim 仍能推送无 request_id 的 `session/queue`，且 revision 单调递增。
- [x] **AC5**：失败响应保持 request_id/ok/error，不产生成功 queue payload。
- [x] **AC6**：重复 request_id 不重复入队，重试返回幂等结果。
- [x] **AC7**：原始 `inbox.json`、`next_turn`、`next_step` 不出现在 WS 输出。
- [x] **AC8**：ftre architecture、lifecycle、startup、全量 pytest、ruff、diff check 通过。
- [x] **AC9**：对 steering 项的 edit/remove 被拒绝并保留 pending；queued 项仍可正常 steer/edit/remove。

## 6. 测试计划

- WebSocket 单测：prompt/steer/edit/remove 成功响应、错误响应、重复 request。
- Inbox 单测：revision 与 wire snapshot 一致、claim 后 revision 增长、持久化失败不返回成功。
- Gateway smoke：连接、发送、Steering、刷新/重连和后台 claim。
- 协议扫描：禁止 `value.accepted` 成功 ACK 和原始 Inbox 字段进入新 Queue Response。

## 7. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-25 | 初始定稿；将 ACK 与 Queue Snapshot 合并，增加服务端 revision | 消除客户端双阶段队列状态和 Steering 状态延迟 |
| 2026-08-25 | 完成 F24：WebSocket 操作成功统一返回 Queue Response；客户端/Inbox/启动回归通过 | 以服务端 revision 作为唯一快照顺序，删除独立 admission ACK |
| 2026-08-25 | 审计补齐 steering 锁定：服务端拒绝对 next-step 用户项的 edit/remove，并增加回归测试 | 与 F22 “steering 在 USER_MESSAGE 前只读”不变量一致，避免客户端锁定被恶意帧绕过 |
