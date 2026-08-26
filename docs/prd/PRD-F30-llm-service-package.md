# PRD-F30 统一 LLM Service Package 与调用管线

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F30 |
| 名称 | 统一 LLM Service Package 与调用管线 |
| 状态 | 草稿 |
| 创建日期 | 2026-08-25 |
| 定稿日期 | 待评审 |
| 验收日期 | 待开发 |
| 关联文档 | `docs/TODO.yaml` F30；`docs/prd/PRD-F28-llm-recovery-plugin.md`；`docs/prd/PRD-F29-llm-stream-fallback-plugin.md`；`E:\deepseek-channel-octo\docs\dsh-llm-analysis.md`；`AGENTS.md` |

---

## 1. 背景与目标

### 1.1 当前问题

当前 ftre 没有真正的 LLM Service。Agent Runtime、Compaction Package、Session Title
分别直接调用 `ftre_agent_core.llm.create_llm_handler()`：

```text
Agent Runtime       ── create_llm_handler() ──┐
Compaction Package ── create_llm_handler() ───┼─ 各自创建 Adapter
Session Title      ── create_llm_handler() ──┘
```

这会产生以下问题：

- Provider 路由没有唯一 Owner；
- Agent、Compaction、Session Title 各自解释 API 配置和错误；
- 流式协议、Token 用量和错误结束语义容易分叉；
- Retry/Fallback 与实际 LLM 调用耦合在不同消费者内；
- `llm/stream` 只有 Hook 合约，没有真实 Service 调用点；
- 消费者无法统一使用模型元数据、默认输出上限和 reasoning 能力；
- 同一请求可能先写一套配置日志，再由另一个工厂重新解析成另一套配置。

### 1.2 DSH 设计依据

DSH 的 `@deepseek-ai/dsh-llm` 将 LLM 定义为：

```text
LlmRuntime = Provider Adapter 注册表
           + 模型解析
           + 单一流式调用入口
           + llm/stream Waterfall
           + StreamChunk / Failure 统一协议
```

它明确不拥有 Retry、Fallback、Compaction、Session、凭据存储或 Agent Loop。

### 1.3 目标

本阶段完成后，ftre 提供一个可独立安装的 `ftre-llm` Package，所有 LLM 消费者统一通过
`ctx.llm` 调用；Provider 通过 Plugin 注册 Adapter；Retry、Fallback、Compaction 和
Session Title 保持独立 Package，不复制 LLM Service 的状态机。

### 1.4 非目标

- 不把 AgentLoop、TurnExecutor、Session 或 Inbox 放入 LLM Package；
- 不在 LLM Service 内实现 Retry、Fallback、Compaction、限流或缓存策略；
- 不让 LLM Service 持有 API Key、用户配置文件或 Session 持久化；
- 不新增 `LlmPort`、`LlmCoordinator`、`LlmFacade` 或第二份 Service Locator；
- 不修改客户端 WebSocket 协议；
- OpenAI Chat Completions/Responses 适配器属于本 Package 的 Provider Plugin；不把它们注册逻辑
  放回 Host Service，也不在本阶段扩展其它供应商协议；
- 不把每个模型、每个请求目的拆成独立 Service。

---

## 2. 目标架构与 Owner

### 2.1 目标目录

```text
E:/ftre/
├─ packages/
│  └─ ftre-llm/
│     ├─ pyproject.toml
│     ├─ README.md
│     ├─ README.zh.md
│     ├─ src/ftre_llm/
│     │  ├─ __init__.py
│     │  ├─ service.py          # LlmService：注册、解析、stream
│     │  ├─ service_adapter.py  # Core 调用形状适配；只透传 StreamChunk
│     │  ├─ contracts.py        # 请求、配置、Provider、PreparedCall
│     │  ├─ base.py              # OpenAIAdapterBase 共享实现骨架（不定义第二契约）
│     │  ├─ events.py            # StreamChunk 统一类型
│     │  ├─ adapters/
│     │  │  ├─ plugin.py         # Provider Plugin：register_adapter + 可逆卸载
│     │  │  ├─ openai_completions.py
│     │  │  └─ openai_responses.py
│     │  └─ errors.py           # LlmFailure 与稳定错误码
│     └─ tests/
│        ├─ test_service.py
│        ├─ test_registration.py
│        ├─ test_prepare_call.py
│        └─ test_stream_protocol.py
│
├─ src/ftre/
│  ├─ services/llm/
│  │  ├─ plugin.py              # Host Provider Plugin，发布 llm Service
│  │  └─ hooks.py               # Host 对 Core Hook 的稳定重导出
│  └─ plugins/builtin/
│     ├─ llm-providers/         # 现有 Provider 的装配适配
│     └─ ...
```

### 2.2 依赖方向

```text
Provider Plugin ───────┐
                       │ inject: llm
                       ▼
                 ftre-llm Service
                       ▲
                       │ inject: llm
┌──────────────────────┼──────────────────────────┐
│                      │                          │
Agent Runtime     Compaction Package       Session Title Package
```

LLM Service 只依赖运行时机制和由 `ftre-llm` 持有的稳定 LLM 协议，不依赖具体 Agent、Session、
Inbox 或客户端。协议实现从旧 Core 迁入后，Core 仅作为待退役运行库保留；Provider 可以可选
注入 `config`/`credentials`，但这些能力不能反向进入 LlmService。

Core 与 Host 使用同一份 `llm/stream` Spec。Agent Runtime 通过
`ftre_llm.LlmServiceAdapter` 进入 Service 时关闭 Service 内层的重复 stream 派发，
因此 Agent 流只包装一次；Compaction/Title 直接调用 Service 时仍由 Service 派发该 Spec。
Agent 创建阶段直接把 Adapter 注入 Core Runner，Core 不先构造空凭据的默认客户端，
且运行中的 Runner 拒绝替换 Adapter。

### 2.3 Owner 表

| 能力 | 唯一 Owner | Service / Hook | 不负责什么 |
|---|---|---|---|
| Provider 路由 | Provider Plugin | `ctx.llm.register_adapter()` | 不负责 Agent Turn |
| LLM 调用 | `ftre-llm` | `ctx.llm.stream()` | 不负责 Retry |
| 模型元数据 | Adapter | `resolve_model_info()` | 不作为请求白名单 |
| Retry 策略 | `ftre-llm-recovery` | Core `llm/error` | 不重发裸 HTTP |
| Fallback | `ftre-llm-fallback` | Host `llm/stream` 包装 | 不拼接两份流 |
| Compaction | `ftre-compaction` | 消费 `ctx.llm.stream()` | 不创建 Adapter |
| Session Title | Session Title Plugin | 消费 `ctx.llm.stream()` | 不读取 Provider 私有配置 |
| Token 计量 | LLM 调用观察 Plugin/Service | `llm/stream` | 不改变流内容 |

---

## 3. 功能需求

### 3.1 LlmService（FR1）

提供唯一稳定 Service key：`llm`。Service 必须支持 Provider 注册、模型解析、调用准备和
流式调用。

必需方法：

```python
class LlmService:
    key = "llm"

    def register_adapter(
        self,
        providers: Sequence[str],
        adapter: "LlmAdapter",
    ) -> "AdapterRegistration":
        """原子注册一组 Provider 路由。"""

    def list_providers(self) -> tuple["ProviderInfo", ...]:
        """返回已注册 Provider 的脱耦元数据。"""

    async def resolve_model_info(
        self,
        provider: str,
        model: str,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> "ModelInfo":
        """解析精确 Provider/Model 的能力，不将 catalog 当白名单。"""

    async def prepare_call(
        self,
        config: "LlmCallConfig",
        *,
        cancellation: asyncio.Event | None = None,
    ) -> "PreparedLlmCall":
        """解析默认值、捕获 Adapter 注册并生成一次性调用句柄。"""

    def stream(
        self,
        request: "LlmRequest",
    ) -> AsyncIterator["StreamChunk"]:
        """唯一的单次 LLM 流式调用入口。"""
```

可选方法（只有 UI/配置页面实际需要时才实现）：

```python
def register_configurable_provider(
    self,
    entry: ConfigurableProvider,
) -> RegistrationHandle: ...

async def list_models(
    self,
    provider: str,
) -> tuple[ModelInfo, ...]: ...

def register_model_discovery(
    self,
    settings_namespace: str,
    discover: ModelDiscovery,
) -> RegistrationHandle: ...

async def discover_models(
    self,
    settings_namespace: str,
    request: ModelDiscoveryRequest,
) -> tuple[DiscoveredModel, ...]: ...
```

`provider_retry_policy()` 不作为消费者必须调用的独立方法；重试策略在
`prepare_call()` 中随 Adapter 注册一起捕获，避免调用方看到与实际 Adapter 不一致的策略。

### 3.2 Adapter（FR2）

Provider Plugin 实现最小 Adapter：

```python
class LlmAdapter(ABC):
    async def stream(
        self,
        request: "LlmRequest",
    ) -> AsyncIterator["StreamChunk"]: ...

    async def resolve_model_info(self, model: str) -> "ModelInfo | None": ...

    def retry_policy(self) -> object | None: ...
```

其中只有 `stream()` 是必须实现的方法，其余能力可以返回空目录或未知元数据。Adapter
不得直接修改 Session、AgentState 或客户端数据。

Provider Plugin 使用示例（具体适配器位于 `ftre-llm` 的 `adapters/`，而不是 Host Service）：

```python
inject = ("llm",)
provide = ()


def apply(ctx: Context, config: Mapping[str, Any] | None = None):
    registration = ctx.llm.register_adapter(
        providers="completions",
        adapter=OpenAICompletionsAdapter,
    )

    # registration 的释放绑定当前 Plugin Fiber；unload 时自动撤销路由。
    ctx.effect(lambda: registration.dispose, label="llm-provider:completions")
```

如果配置热更新改变路由，必须调用：

```python
registration.replace(("opencode", "opencode-backup"))
```

不得先 dispose 再 register，避免路由短暂消失。

### 3.3 调用配置（FR3）

```python
@dataclass(frozen=True, slots=True)
class LlmCallConfig:
    provider: str
    model: str
    reasoning_effort: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    stop: tuple[str, ...] = ()
```

Host 的 `LLMConfig` 也必须保留同名的稳定 `provider` 字段。任何消费者不得在字段缺失时
填入 `configured`、`default` 等伪 Provider；若 Provider 为空，应在发起调用前报告配置缺失。

该对象代表会话/轮次的模型选择和采样配置，应该写入 Session 的 request header。它不包含
消息内容和 API Key。

### 3.4 完整请求（FR4）

```python
@dataclass(frozen=True, slots=True)
class LlmRequest:
    config: LlmCallConfig
    messages: tuple["LlmMessage", ...]
    system: str | None = None
    tools: tuple["ToolSchema", ...] = ()
    session_id: str | None = None
    turn_id: str | None = None
    purpose: Literal["conversation", "compaction", "session-title"] = "conversation"
    cancellation: asyncio.Event | None = None
```

`messages`、`tools`、`config` 在分发前必须复制并冻结。Hook 和 Adapter 只能读取，不能原地
修改调用方的历史对象。

### 3.5 Prepared Call（FR5）

```python
@dataclass(frozen=True, slots=True)
class PreparedLlmCall:
    config: LlmCallConfig
    model_info: "ModelInfo | None"
    retry_policy: "RetryPolicy"
    adapter_defaults: frozenset[str]
    stream: Callable[[LlmRequest], AsyncIterator["StreamChunk"]]
```

`prepare_call()` 必须完成：

```text
捕获当前 Adapter registration
        ↓
解析精确模型能力和默认 max_tokens/reasoning_effort
        ↓
复制并冻结最终配置
        ↓
返回一次性 PreparedLlmCall
```

硬约束：

- `PreparedLlmCall.stream()` 只能调用一次；
- 调用时的 `LlmCallConfig` 必须与 `prepared.config` 相等；
- Adapter 热更新不能让已经准备的调用换到另一实例；
- 不支持的 reasoning effort 必须在 Provider I/O 前失败；
- API Key 不进入 `LlmCallConfig`、Hook Payload 或 Session 日志。

### 3.6 StreamChunk 协议（FR6）

将旧 `ftre-agent-core` 的公开 LLM Chunk 类型迁入 `ftre-llm`，由新 Package 成为唯一协议 Owner；
不让 Package 反向依赖待退役 Core，也不创建第三套相似 DTO：

```text
BlockStart
TextDeltaChunk
ReasoningDeltaChunk
ToolCallDeltaChunk
BlockEnd
UsageChunk
FinishChunk
```

顺序约定：

```text
0..N 个内容分片
        ↓
UsageChunk（可选）
        ↓
唯一 FinishChunk
```

`FinishChunk.reason`：

```python
Literal["stop", "tool_calls", "max_tokens", "error", "aborted"]
```

Provider 选择失败、Adapter 构造失败、HTTP 失败和迭代失败都必须归一为：

```python
FinishChunk(
    reason=FinishReason(
        kind="error",
        failure=LlmFailure(
            code="TIMEOUT",
            message="provider request timed out",
        ),
    )
)
```

Middleware、下游消费者和清理逻辑自身的编程错误继续抛出，不伪装成 Provider 失败。

### 3.7 LLM Service 调用示例（FR7）

普通 Agent/Feature 消费者只能注入 `llm`：

```python
inject = ("llm",)


async def generate_title(ctx: Context, text: str) -> str:
    config = LlmCallConfig(
        provider="opencode",
        model="deepseek-v4-flash",
        max_tokens=128,
    )
    prepared = await ctx.llm.prepare_call(config)

    request = LlmRequest(
        config=prepared.config,
        messages=(
            LlmMessage.user(text),
        ),
        purpose="session-title",
    )

    assembler = BlockAssembler()
    async for chunk in prepared.stream(request):
        assembler.push(chunk)

    if assembler.finish.kind in {"error", "aborted"}:
        raise LlmRequestError(assembler.finish.failure)

    return assembler.text()
```

一次性辅助调用也可以直接使用：

```python
async for chunk in ctx.llm.stream(
    LlmRequest(
        config=config,
        messages=messages,
        purpose="compaction",
    )
):
    assembler.push(chunk)
```

Agent 主循环必须使用 `prepare_call()`，确保最终配置先进入 request header，再发起模型请求。
Compaction/Session Title 等辅助调用可以直接使用 `stream()`，但仍必须声明 `purpose`。

---

## 4. Hook 定义

### 4.1 `llm/stream`（FR8）

模式：Waterfall。

```python
@dataclass(frozen=True, slots=True)
class LlmStreamPayload:
    agent_id: str
    session_id: str
    turn_id: str
    provider: str
    model: str
    purpose: str
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...]
    cancellation: asyncio.Event
    attempt: int
    max_attempts: int
    invoke: Callable[[], AsyncIterator[StreamChunk]]
```

输入是本次单独调用的冻结快照，输出是：

```python
AsyncIterator[StreamChunk]
```

调用链：

```text
ctx.llm.stream(request)
        ↓
llm/stream waterfall
        ↓
listener(payload, next)
        ↓
next() → Adapter.stream()
```

允许的用途：

- 日志和 Token 计量；
- Stream 包装；
- Replay；
- 缓存；
- 辅助调用的零输出 Fallback。

禁止：

- 已产生正文、Reasoning 或 Tool Call 后切换模型；
- 在流中执行完整 Retry；
- 修改 `messages`、`tools` 或持久化 Session；
- 隐式创建第二个 HookRuntime。

### 4.2 `agent/request`（FR9）

模式：Waterfall。

```python
@dataclass(frozen=True, slots=True)
class AgentRequestPayload:
    agent_id: str
    session_id: str
    turn_id: str
    step: int
    config: LlmCallConfig
    previous_failure: LlmFailure | None
    cancellation: asyncio.Event
```

输入：本次请求准备使用的配置。输出：

```python
LlmCallConfig
```

用途：

- `/model` 修改模型；
- Fallback Plugin 应用备用路由；
- Provider 路由策略；
- Agent/Session 级模型选择。

示例：

```python
async def choose_backup(payload, next_):
    config = await next_()
    if payload.previous_failure is not None:
        return replace(
            config,
            provider="opencode-backup",
            model="deepseek-v4-pro",
        )
    return config
```

### 4.3 `llm/error`（FR10）

模式：Waterfall。

```python
@dataclass(frozen=True, slots=True)
class LLMErrorPayload:
    agent_id: str
    session_id: str
    turn_id: str
    iteration: int
    model: str
    error_code: str
    error_message: str
    attempt: int
    max_attempts: int
    cancellation: asyncio.Event
```

返回：

```python
LLMErrorDecision | None
```

`None` 表示当前 Plugin 不认领，继续 Hook 链；`LLMErrorDecision` 只表达 retry/stop 和
退避建议，实际 RetryEvent、重建请求、取消和次数上限仍由 Core Runner 执行。

### 4.4 `llm/adapters-updated`（FR11）

模式：Emit，无返回值。

触发时机：

- Adapter 注册；
- Adapter dispose；
- Provider 路由原子替换；
- 可配置 Provider 目录变化。

消费者收到事件后重新调用 `list_providers()` 或 `list_models()`，不自行轮询。

### 4.5 Hook 数量约束

本阶段不新增以下重复 Hook：

- `llm/attempt-failed`；
- `llm/retry-decision`；
- `llm/fallback`；
- `llm/stream-before`；
- `llm/stream-after`。

Retry 的业务裁决统一落在 Core `llm/error`；Fallback 的零输出流接管落在 Host `llm/stream`。
LlmService 不再发布返回值无人消费的平行错误 Hook，避免两套 Retry 决策 Owner。

---

## 5. Retry 设计

### 5.1 Owner

Retry 独立为 `ftre-llm-retry` Package，LlmService 不拥有 Retry 状态。

```text
LlmService 只执行一次请求
Retry Plugin 监听 Core llm/error
Core Runner 执行 LLMErrorDecision
```

### 5.2 策略数据

```python
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    mode: Literal["normal", "always"]
    max_retries: int
    retryable_codes: frozenset[str]
    initial_delay_ms: int
    max_delay_ms: int
    jitter_ratio: float
```

默认可重试：

```text
RATE_LIMIT
SERVER
TIMEOUT
TRANSPORT
EMPTY_RESPONSE
```

默认不可重试：

```text
INVALID_CREDENTIAL
QUOTA_EXCEEDED
CONTEXT_WINDOW_EXCEEDED
UNSUPPORTED_REASONING_EFFORT
```

### 5.3 执行流程

```text
LLM Stream 失败
      ↓
Core Runner 发布 llm/error
      ↓
Retry Plugin 判断 code + attempt + policy
      ↓
写入 retry planned
      ↓
可取消退避
      ↓
写入 retry started
      ↓
返回 LLMErrorDecision
      ↓
Core Runner 重新 build request
      ↓
agent/request → prepare_call → request/header → llm/stream
```

硬约束：

- Retry 次数从 Session 日志恢复，不只保存在内存；
- `retry planned` 必须先于等待持久化；
- 等待期间取消或 unload 必须清理；
- Retry 必须重新构建请求，不能裸重放旧 Adapter 流；
- Context Window 溢出交给 Compaction，不进入普通 Retry；
- 一次 Retry 只能由一个 Owner 认领。

### 5.4 Retry 日志模型

```python
{
    "event": "llm/retry",
    "retry_id": "retry-opaque-id",
    "session_id": "session-id",
    "turn_id": "turn-id",
    "step": 2,
    "provider": "opencode",
    "model": "deepseek-v4-flash",
    "failure_code": "TIMEOUT",
    "attempt": 1,
    "max_attempts": 3,
    "delay_ms": 800,
    "status": "planned",
}
```

API Key、完整 Prompt 和原始异常对象不得写入 Retry 日志。

---

## 6. Fallback 设计

### 6.1 主要方案：完整请求级路由切换

Fallback 独立为 `ftre-llm-fallback` Package，优先使用两个 Agent Hook：

```text
主模型失败
      ↓
普通 Retry 已耗尽
      ↓
Fallback Plugin 包装 Host llm/stream
      ↓
零输出时创建备用模型流
      ↓
重新 prepare_call + stream 完整请求
```

Fallback 不应该把两个模型的半截流拼接在一起。

Fallback 条件：

- 普通 Retry 已耗尽；
- 错误码命中白名单；
- 没有取消；
- 当前请求尚未产生可持久化的成功 Assistant 输出；
- 备用路由存在且可解析；
- 同一 Turn 最多切换一次，不能递归 fallback。

### 6.2 辅助方案：`llm/stream` 零输出切换

Compaction/Session Title 等辅助调用可以使用低级流包装：

```text
最后一次 attempt
主模型零输出失败
错误码命中白名单
      ↓
关闭主流
      ↓
创建备用 Adapter（max_retries=0）
      ↓
输出备用流
```

限制：

- 主模型已输出正文、Reasoning、Tool Call 或 BlockStart 后不得切换；
- 取消时不得切换；
- overflow/context window 不得被 Fallback 接管；
- 备用失败时返回原始主错误；
- 备用 Adapter 不再次进入 Retry/Fallback Hook。

### 6.3 Fallback 状态

Fallback 状态属于单个 Turn，不是全局 Provider 状态：

```python
@dataclass(frozen=True, slots=True)
class FallbackSelection:
    original_provider: str
    original_model: str
    fallback_provider: str
    fallback_model: str
    reason_code: str
    selected_at_attempt: int
```

选中备用路由后必须把生效的 `LlmCallConfig` 写入下一次 `request/header`，并在日志中记录
路由变更原因，但不记录凭据。

---

## 7. 消费者迁移

### 7.1 Agent Runtime

当前：

```python
adapter = create_llm_handler(api_type, **kwargs)
stream = adapter.stream(messages, tools)
```

目标：

```python
prepared = await ctx.llm.prepare_call(call_config)
request = LlmRequest(
    config=prepared.config,
    messages=messages,
    tools=tools,
    session_id=session_id,
    turn_id=turn_id,
    purpose="conversation",
    cancellation=cancellation,
)

async for chunk in prepared.stream(request):
    assembler.push(chunk)
```

Agent Runtime 负责：

- 发布 `agent/request`；
- 调用 `prepare_call()`；
- 写 request/header；
- 调用 `stream()`；
- Core 发布 `llm/error` 并执行 `LLMErrorDecision`；
- Host LlmService 不重复发布失败决策 Hook。

Agent Runtime 不负责：

- Provider Adapter 创建；
- Fallback 选择；
- Retry 次数计算；
- LLM API Key；
- StreamChunk 拼接以外的 Provider 逻辑。

### 7.2 Compaction

```python
prepared = await ctx.llm.prepare_call(
    LlmCallConfig(
        provider=config.provider,
        model=config.model,
        max_tokens=config.max_tokens,
    )
)

request = LlmRequest(
    config=prepared.config,
    messages=messages,
    system=system,
    tools=tools,
    purpose="compaction",
)

async for chunk in prepared.stream(request):
    assembler.push(chunk)
```

Compaction 不允许再 import `create_llm_handler()`。

### 7.3 Session Title

与 Compaction 相同，只改变：

```python
purpose="session-title"
```

不允许 Session Title 自己实现另一套错误归一化和重试。

---

## 8. 错误和资源语义

### 8.1 稳定错误码

至少统一以下错误码：

```text
NO_ADAPTER
DUPLICATE_ADAPTER
INVALID_ADAPTER
INVALID_MODEL_INFO
UNSUPPORTED_REASONING_EFFORT
INVALID_PREPARED_CALL
MISSING_CREDENTIAL
INVALID_CREDENTIAL
RATE_LIMIT
SERVER
TIMEOUT
TRANSPORT
ABORTED
CONTEXT_WINDOW_EXCEEDED
QUOTA_EXCEEDED
EMPTY_RESPONSE
```

### 8.2 错误边界

```text
Provider/HTTP/迭代失败       → FinishChunk(error/aborted)
LLM Service 配置错误          → LlmFailure / LlmError
llm/stream Plugin 自身异常     → 继续抛出
下游消费者异常                → 继续抛出
资源清理异常                  → 记录并按生命周期策略处理
```

### 8.3 取消

- `LlmRequest.cancellation` 是请求级取消信号；
- Adapter 必须尽快响应；
- Service unload 时必须关闭进行中的流；
- 取消不得触发 Retry 或 Fallback；
- `aborted` 与 `error` 必须在 `FinishChunk` 中区分。

---

## 9. 非功能需求

- **可卸载**：Provider、Hook、Adapter、后台任务均绑定 Plugin Fiber；卸载后路由和监听器
  完整消失；
- **原子性**：Provider 注册替换无可观察空窗；
- **不可变**：Request、Config、Hook Payload 和 Prepared Call 在分发前冻结；
- **安全**：API Key 不进入 Request、Hook、Session、Retry 日志和异常 message；
- **并发**：不同 Session 可并行；同一 Prepared Call 只能 dispatch 一次；
- **可观测**：每次请求可以通过 session/turn/step/attempt 关联日志，但不记录 Prompt 全文；
- **兼容**：未安装可选 Retry/Fallback 时，普通 `ctx.llm.stream()` 仍可执行；
- **最小依赖**：LlmService 不依赖 Session、Inbox、Agent、Compaction 或客户端。

---

## 10. 测试计划

### 10.1 Service/Adapter

- Provider 路由注册、重复注册、原子替换、卸载；
- 未注册 Provider 返回 `NO_ADAPTER`；
- 模型元数据解析和未知模型；
- reasoning effort、max tokens 默认值；
- Prepared Call 只能调用一次；
- Adapter 热更新时 Prepared Call 仍绑定原 Adapter；
- API Key 不出现在异常和日志。

### 10.2 Stream 协议

- 文本、Reasoning、Tool Call、Usage、Finish 顺序；
- 直接异常和错误 FinishChunk 统一；
- 取消返回 `aborted`；
- 流结束后无额外 chunk；
- `BlockAssembler` 只组装一次，不重复拼接。

### 10.3 Hook

- `llm/stream` listener 能包装和放行流；
- listener 不调用 `next()` 时短路行为明确；
- `agent/request` 能修改 provider/model；
- `llm/error` 未认领时继续 Core 默认路径；
- Hook unload/restart/in-flight 资源清理；
- 不存在重复 Retry Hook 或第二份 LLM Stream Spec。

### 10.4 Retry/Fallback

- 前 N-1 次失败只进入 Retry；
- Retry 计数从 Session 日志恢复；
- durable-before-wait；
- 取消、unload、重启期间等待清理；
- 最后一次失败才允许 Fallback；
- 已有正文/Reasoning/Tool Call 时不切换；
- 备用模型失败不递归；
- overflow 交给 Compaction；
- Fallback 路由变更进入下一次 request/header。

### 10.5 消费者迁移

- Agent Runtime 不再直接调用 `create_llm_handler()`；
- Compaction 不再直接创建 Adapter；
- Session Title 不再直接创建 Adapter；
- 三类调用都通过 `ctx.llm.stream()`；
- 无 Retry/Fallback Package 时基础调用仍可运行；
- 默认 Composition、禁用 Package、洁净安装和 wheel 通过。

---

## 11. 验收标准

- [ ] **AC1**：存在独立 `packages/ftre-llm`，提供唯一 `llm` Service 和 entry point。
- [ ] **AC2**：Provider Plugin 只能通过 `register_adapter()` 注册 Adapter；Agent、Compaction、
  Session Title 不再直接调用 `create_llm_handler()`。
- [ ] **AC3**：`stream()` 返回统一 `StreamChunk`，所有 Provider 请求失败都以唯一终止
  `FinishChunk(error/aborted)` 表示。
- [ ] **AC4**：`prepare_call()` 能解析并冻结最终配置、模型元数据和 Adapter 注册；调用句柄
  不可复用。
- [ ] **AC5**：`llm/stream`、`agent/request`、`llm/error` 和
  `llm/adapters-updated` 的入参、出参、Waterfall/Emit 语义有契约测试。
- [ ] **AC6**：LlmService 本体不依赖 Session、Inbox、Agent、Compaction、Client 或凭据存储。
- [ ] **AC7**：Retry 只由独立 Package 在 `llm/error` 提供决策；计数持久化、退避可取消、
  重试重新构建完整请求。
- [ ] **AC8**：Fallback 只在 Retry 耗尽后切换完整请求；低级流 Fallback 只允许零输出场景，
  不递归、不拼接半截流。
- [ ] **AC9**：未安装/禁用 Retry、Fallback、Compaction 时，基础 LLM 调用仍能启动和运行。
- [ ] **AC10**：Provider unload/restart、in-flight 流、取消、原子替换和 API Key 脱敏通过。
- [ ] **AC11**：Core、Host、Package 全量 pytest、ruff、wheel、洁净安装和 Gateway smoke 通过。
- [ ] **AC12**：更新 TODO、CHANGELOG、执行报告；无死代码、兼容壳、重复 Owner、缓存或空
  Package 遗留。

---

## 12. 实施批次

| 批次 | 内容 | 结果 |
|---|---|---|
| F30.1 | 冻结 `LlmCallConfig`、`LlmRequest`、`LlmFailure`、`StreamChunk`、Adapter 契约 | LLM Contract 基线 |
| F30.2 | 实现 `ftre-llm` Service、Provider Registry、原子注册和模型解析 | 统一 `ctx.llm` |
| F30.3 | 接入真实 Provider，迁移 Agent Runtime、Compaction、Session Title | 删除三处直接 Handler 创建 |
| F30.4 | 收敛 `llm/stream`、`agent/request`、`llm/error` Hook | Hook 单一 Owner |
| F30.5 | Retry Package durable-before-wait 与完整请求重建 | 可恢复 Retry |
| F30.6 | Fallback 完整请求路由切换与辅助调用零输出 Fallback | 不拼接半截流 |
| F30.7 | 生命周期、错误、取消、洁净安装、全量验收 | PRD 收尾 |

---

## 13. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-25 | 初始草案：根据 DSH `dsh-llm` 分析建立 ftre 统一 LLM Service、Adapter、Stream、Hook、Retry/Fallback 方案 | 当前 Agent、Compaction、Session Title 各自创建 Handler，缺少唯一调用入口 |
| 2026-08-26 | 收敛 Adapter Owner：`contracts.py` 唯一声明 `LlmAdapter`，`base.py` 仅保留 OpenAI 共享骨架；新增 `ftre_llm.adapters.plugin`，通过 `LlmService.register_adapter()` 注册 Completions/Responses，Host `llm-service` 不再 import concrete adapter | 消除两个 Adapter 契约和 Host 越权注册，确保 Provider 可独立卸载 |
| 2026-08-26 | `LLMConfig` 增加稳定 `provider` 字段；Compaction、Session Title、Agent Runtime 禁止使用 `configured` 伪 Provider；`LlmService` 在 register/replace/dispose 时异步发布 `llm/adapters-updated` | 保证路由、Hook、日志和诊断使用真实 Provider，并闭合适配器生命周期通知 |
| 2026-08-26 | `ftre-llm.events` 成为迁入后的 StreamChunk 唯一 Owner；Core `llm.events` 改为重导出该协议，删除 Host `core_bridge.py` 和输出转换；Retry 统一接入 Core `llm/error`，移除 LlmService 中返回值无人消费的 `agent/request-error` | 消除运行时两套 Chunk 类型和无效 Retry 决策链 |
