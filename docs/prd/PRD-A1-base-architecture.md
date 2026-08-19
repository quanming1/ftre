# PRD-A1-基础架构

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | A1 |
| 名称 | 基础架构（Channel + EventBus + AgentLoop + config + main） |
| 状态 | 已验收 |
| 创建日期 | 2026-08-12 |
| 定稿日期 | 2026-08-12 |
| 验收日期 | 2026-08-12 |
| 关联文档 | docs/TODO.yaml 阶段 A1；AGENTS.md |

## 1. 背景与目标

- **背景**：ftre Gateway 需要基础骨架，Channel 接收外部输入（WebSocket、HTTP、插件），EventBus 传输类型化消息，AgentLoop 消费执行。这是整个系统的地基，后续所有功能都建立在这三者之上。
- **目标**：完成 `ftre gateway` CLI 入口启动后端，监听 WebSocket 连接，正确加载 config.json 配置，形成 Channel → EventBus → AgentLoop 的消息流转闭环；后续 B1 在 AgentLoop 内扩展 SessionLane，而不改变这条入口边界。
- **非目标**：不实现具体工具体系（A3）、不实现 Session 持久化（A2）、不实现插件架构（C1）。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：Channel 抽象——定义 `Channel` 基类，统一接收外部输入和发送下行消息的接口
- [x] FR2：EventBus inbound/outbound 队列——实现 `EventBus`，支持 inbound（外部→AgentLoop）和 outbound（AgentLoop→外部）消息传输
- [x] FR3：AgentLoop 消费循环——实现 `AgentLoop`，从 EventBus inbound 队列消费消息并交给内部执行编排（当前由 B1 的 SessionLane 负责按 session 串行执行）
- [x] FR4：config.json 加载——实现 `config.py`，从 `~/.ftre/config.json` 加载全局配置（LLM、tools、workspace 等）
- [x] FR5：CLI 入口 `ftre gateway`——实现 `main.py`，注册 `ftre gateway` 命令，启动 Gateway 后端进程
- [x] FR6：config 读取 model 级 LLM 协议（`api_type`）——`_build_llm_config` 解析优先级为 model 条目 `api_type` > provider 级 `api_type` > 默认 `"completions"`；`LLMConfig.api_type` 正确传导至 ReActAgent（同一 provider 内按模型混合协议，如 OpenCode Go 的 Muse/Luna 走 responses、其余走 completions）

### 2.2 非功能需求

- 性能：Gateway 启动时间 < 3 秒
- 安全：config 中不包含明文密钥（api_key 通过环境变量注入）
- 兼容性：Python 3.12+

## 3. 技术方案

### 模块设计

| 文件 | 职责 |
|---|---|
| `src/ftre/channel/base.py` | `Channel` 抽象基类，定义 `receive()` / `send()` 接口 |
| `src/ftre/bus/bus.py` | `EventBus`，inbound/outbound asyncio 队列 + 消息分发 |
| `src/ftre/agent/loop.py` | `AgentLoop`，消费 inbound、按 session 路由到内部编排、统一发布 outbound |
| `src/ftre/config.py` | 全局配置加载，`config.json` 解析为 `Config` dataclass |
| `src/ftre/main.py` | Typer CLI 入口，`ftre gateway` 启动 FastAPI + WS 服务 |

### 关键数据结构

```python
@dataclass
class Config:
    llm: LLMConfig          # provider, model, api_key, base_url
    workspace: str          # 默认工作区路径
    tools: list[str]        # 启用的工具白名单

class Channel(ABC):
    @abstractmethod
    async def receive(self) -> InboundMessage: ...
    @abstractmethod
    async def send(self, message: OutboundMessage) -> None: ...
```

## 4. 后续扩展边界

A1 只定义三件套的基础通信，不把 session 队列或压缩逻辑塞进 EventBus：

```mermaid
flowchart LR
    CH["Channel"] --> BUS["EventBus"]
    BUS --> LOOP["AgentLoop"]
    LOOP --> LANE["SessionLane（B1）"]
    LANE --> EXEC["TurnExecutor（B3）"]
```

- Channel 负责输入输出适配；EventBus 负责消息传输和 request/reply；AgentLoop 负责消费、路由和统一下行。
- SessionLane、MailboxStore、ContextGate 和 CompletionRegistry 是 AgentLoop 的内部扩展，不改变 A1 的外部接口。
- 因此 A1 的 Bus ACK 只表示“请求已被内部编排接纳”；Turn 的运行状态和完成结果由 B1/B3 的协议发布。

### 4.1 基础接口

- `Channel.receive(session_id, data, metadata, kind="user_message")`：把外部输入归一成 `BusMessage`，通过 `EventBus.request_inbound` 等待明确结果。
- `EventBus.request_inbound(msg)`：只负责内存队列传输和一次 request/reply 关联；Bus 停止时唤醒所有等待者并拒绝新请求。
- `AgentLoop._consume()`：消费全局 inbound；不直接执行 Agent，交给 B1 的 SessionLaneRegistry。
- `EventBus.publish_outbound(msg)`：将 Projection/SessionLane 产生的下行消息交给 ChannelManager；不承担历史重放，attach 快照由 Channel + Projection/AgentLoop 查询接口完成。

### 4.2 生命周期边界

- Gateway 启动顺序：SessionManager 加载 state → AgentLoop 启动并恢复 Lane → Channel 开放入口。
- Gateway 停止顺序：停止新的 inbound admission → 取消/等待 Lane 和真实 compact → 停止 Channel；未领取 pending 保留在磁盘。
- EventBus 内存队列不是崩溃恢复机制；需要恢复的业务数据必须先进入 A2 的 state.json。

## 5. 验收标准

- [x] AC1：执行 `ftre gateway` 启动后端，日志输出监听端口
- [x] AC2：WebSocket 客户端可连接到 Gateway 并建立会话
- [x] AC3：`config.json` 中的 LLM 配置正确加载，agent 可使用配置的 provider/model
- [ ] AC4：model 条目配置 `"api_type": "responses"` 的模型（如 muse-spark-1.2），`_build_llm_config` 返回的 `LLMConfig.api_type == "responses"` 且传导至 ReActAgent；未配置时回退 provider 级，再回退 `"completions"`（自动化测试断言三级回退 + 真实对话回归见 B2 PRD AC6）

## 6. 测试计划

- `tests/test_bus_request_reply.py`：inbound request/reply、Bus stop 和等待者唤醒。
- Gateway 启动/停止手动验收：先恢复 Session，再开放 WS；停止后新用户输入得到明确失败，磁盘 pending 不丢。
- 不同 session 并发提交：确认 Bus 全局消费不会把 SessionLane 的同 session 串行边界扩大成全局串行。

## 7. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-08-13 | 补充 AgentLoop 向 SessionLane/TurnExecutor 的扩展边界，修正 FR3 的“直接执行”表述 | 让基础架构 PRD 与 B1/B3 的职责拆分保持一致 |
| 2026-08-13 | 补充 Channel/EventBus/AgentLoop 接口、启动停止顺序和内存 Bus 的可靠性边界；增加测试计划 | 让 A1 成为后续 A2/B1/B3 的稳定基础契约，而不是只描述三件套名称 |
| 2026-08-13 | 影响复核：仅补充文档，不改变代码和 A1 行为；AC1-AC3 不受影响 | 记录本次 PRD 反推的验收影响 |
| 2026-08-18 | 新增 FR6 / AC4：config 解析 model 级 `api_type`（优先级 model 条目 > provider 级 > 默认 completions）并传导至 ReActAgent。代码实现随 ftre-agent-core B2（LLM 协议适配层，PRD-B2-llm-adapter）的 Phase 1 管道落地；原 AC1-AC3 不受影响 | OpenCode Go 等混合协议 provider 出现：同一 provider 内 Muse/Luna 走 responses、其余走 completions，reasoning_effort 仅 responses 路径生效（实测），协议必须按模型粒度配置 |
