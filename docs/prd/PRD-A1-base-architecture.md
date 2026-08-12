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
- **目标**：完成 `ftre gateway` CLI 入口启动后端，监听 WebSocket 连接，正确加载 config.json 配置，形成 Channel → EventBus → AgentLoop 的消息流转闭环。
- **非目标**：不实现具体工具体系（A3）、不实现 Session 持久化（A2）、不实现插件架构（C1）。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：Channel 抽象——定义 `Channel` 基类，统一接收外部输入和发送下行消息的接口
- [x] FR2：EventBus inbound/outbound 队列——实现 `EventBus`，支持 inbound（外部→AgentLoop）和 outbound（AgentLoop→外部）消息传输
- [x] FR3：AgentLoop 消费循环——实现 `AgentLoop`，从 EventBus inbound 队列消费消息并执行 agent 循环
- [x] FR4：config.json 加载——实现 `config.py`，从 `~/.ftre/config.json` 加载全局配置（LLM、tools、workspace 等）
- [x] FR5：CLI 入口 `ftre gateway`——实现 `main.py`，注册 `ftre gateway` 命令，启动 Gateway 后端进程

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
| `src/ftre/agent/loop.py` | `AgentLoop`，消费 inbound 消息、调用 agent、发布 outbound |
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

## 5. 验收标准

- [x] AC1：执行 `ftre gateway` 启动后端，日志输出监听端口
- [x] AC2：WebSocket 客户端可连接到 Gateway 并建立会话
- [x] AC3：`config.json` 中的 LLM 配置正确加载，agent 可使用配置的 provider/model
