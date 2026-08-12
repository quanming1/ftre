# PRD-A4-BusChannel协议

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | A4 |
| 名称 | Bus + Channel 协议（TypedBusMessage + ws_channel + subagent_channel） |
| 状态 | 已验收 |
| 创建日期 | 2026-08-12 |
| 定稿日期 | 2026-08-12 |
| 验收日期 | 2026-08-12 |
| 关联文档 | docs/TODO.yaml 阶段 A4；AGENTS.md |

## 1. 背景与目标

- **背景**：A1 的 EventBus 是基础实现，缺乏强类型消息信封和耐久接纳语义。随着 WS Channel 和 subagent Channel 的引入，需要统一的 typed 消息协议，确保消息类型校验、request/reply 的 ACK 确认。
- **目标**：实现 `TypedBusMessage` 强类型信封、`BusMessage` type 白名单、request/reply 耐久接纳、ws_channel 和 subagent_channel 两个 Channel 适配器。
- **非目标**：不实现 SessionLane 并发模型（B1）、不实现 HTTP Channel。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：TypedBusMessage 强类型信封——每条 Bus 消息包含 type、payload、metadata，类型在运行时校验
- [x] FR2：BusMessage type 白名单——定义合法消息类型枚举，非法类型在入队时拒绝
- [x] FR3：InboundMetadata / OutboundMetadata——携带 session_id、request_id、channel_id 等路由信息
- [x] FR4：request/reply 耐久接纳——`request_inbound` 提交后等待 ACK 确认，未确认时发送方保留状态
- [x] FR5：ws_channel frame_id→request_id 转换——WebSocket 帧的 frame_id 映射为内部 request_id
- [x] FR6：subagent_channel——为 subagent session 提供 Channel 适配，支持 task 工具派发的子任务通信

### 2.2 非功能需求

- 性能：Bus 消息分发延迟 < 1ms（内存队列）
- 安全：消息 type 白名单防止注入非法消息类型
- 兼容性：新消息类型可通过白名单扩展，不影响已有类型

## 3. 技术方案

### 模块设计

| 文件 | 职责 |
|---|---|
| `src/ftre/bus/bus.py` | `EventBus`，inbound/outbound 队列 + 消息分发 |
| `src/ftre/bus/message.py` | `TypedBusMessage` 强类型信封定义 |
| `src/ftre/bus/protocol.py` | `BusMessage` type 白名单 + request/reply 协议 |
| `src/ftre/bus/payloads.py` | `InboundMetadata` / `OutboundMetadata` 等载荷定义 |
| `src/ftre/channel/ws_channel.py` | WebSocket Channel 适配，frame_id→request_id 转换 |
| `src/ftre/channel/subagent_channel.py` | Subagent Channel 适配 |

### 关键数据结构

```python
@dataclass
class TypedBusMessage:
    type: BusMessageType        # 白名单枚举
    payload: dict              # 消息体
    metadata: InboundMetadata | OutboundMetadata

class BusMessageType(Enum):
    REQUEST_INBOUND = "request_inbound"
    REPLY_OUTBOUND = "reply_outbound"
    STREAM_EVENT = "stream_event"
    # ...
```

## 5. 验收标准

- [x] AC1：Bus 消息类型校验——提交非法 type 的消息被 EventBus 拒绝
- [x] AC2：request_inbound 等待 ACK——`request_inbound` 提交后发送方等待接收方 ACK，超时有明确错误
- [x] AC3：ws_channel admission ack 正确——WebSocket 帧到达后 ws_channel 正确转换 frame_id 为 request_id 并回 ACK
