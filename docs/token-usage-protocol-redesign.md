# FTRE Token Usage 协议重构设计与多端实施计划

> 状态：待实施  
> 日期：2026-07-28  
> 涉及仓库：`ftre-agent-core`、`ftre`、`ftre-desktop`  
> 兼容策略：测试阶段破坏性升级，不兼容旧会话数据，不提供迁移逻辑

## 1. 文档目的

本文档定义 FTRE Token Usage 的最终数据模型、事件协议、持久化结构、
上下文水位算法和多仓库实施计划。

文档面向后续执行改造的开发者或 Agent，应当能够在不依赖本次讨论记录的
情况下完成 Core、Gateway 后端和 Desktop 客户端的联动修改。

本次只编写设计文档，不修改业务代码。

---

## 2. 原始需求与决策上下文

### 2.1 原始问题

Desktop 在会话
`ws_sess_7da008312382` 中显示：

```text
实际（LLM 上报）  1,104,200
估算（未实算部分）       ≈ 0
合计              1,104,200
上下文窗口        1,000,000
占比                    100%
```

用户判断该会话的真实上下文不可能超过一百万 Token，要求检查
`state.json`、后端统计逻辑和客户端展示逻辑。

### 2.2 真实数据结论

该会话最后一个 assistant Reply 内包含 23 次 LLM Call。

23 次调用累计：

```text
prompt_tokens 累计       1,101,978
completion_tokens 累计       2,222
累计消费                 1,104,200
```

最后一次调用：

```text
prompt_tokens               58,868
completion_tokens              112
total_tokens                58,980
```

因此客户端显示的 `1,104,200` 是一个 Reply 内 23 次模型调用的累计消费，
不是当前上下文大小。

当前 `summary + tail` 的本地字符估算约为 `43,686` Token；Trace 中最后一次
模型调用的真实 `total_tokens` 为 `58,980`。两者的差值主要来自系统提示词、
工具 Schema 和运行时注入内容。

### 2.3 已造成的实际影响

错误统计不仅影响 UI，还影响自动压缩。

同一会话曾记录：

```json
{
  "trigger": "auto",
  "tokens_before": 5915370,
  "trigger_ratio": 5.91537,
  "tokens_after": 3517
}
```

当时一个超长 Reply 的累计消费为约 `5.9M`，但该 Reply 最后一次模型调用
只有约 `106,979` Token。对于 `1,000,000` 上下文窗口，真实比例约为
`10.7%`，不应在 `60%` 阈值触发压缩。

### 2.4 QwenPaw 对照结论

QwenPaw 同样使用 AgentScope。真实持久化数据表明它区分三种语义：

```text
AgentScope Msg.usage 累计消费       53,575
最后一次模型调用                    18,105
AgentState 当前内容字符估算           5,551
```

QwenPaw 的实现：

- 每次模型调用都进入独立的全局消费统计；
- session 内只暂存最后一次调用的 usage；
- 当前上下文水位重新估算 `AgentState.summary + AgentState.context`；
- 两组数据写入 closing assistant Msg 的
  `metadata.qwenpaw_turn_usage`。

QwenPaw 避免了把累计消费当成上下文，但字符估算会遗漏系统提示词和工具
Schema，真实数据中 `5,551` 明显低于最后一次 API 上报的 `18,105`。

FTRE 借鉴其“分离累计消费与上下文水位”的原则，但使用强类型字段，并优先
使用最后一次 LLM 实报值作为上下文锚点。

### 2.5 官方接口实测

使用 `C:\Users\蒋全明\.ftre\config.json` 中的配置，通过 `curl.exe`
分别调用 DeepSeek 官方和美团 LongCat 官方的 OpenAI-compatible
`/chat/completions` 接口。

DeepSeek `deepseek-v4-flash` 返回：

```json
{
  "prompt_tokens": 8,
  "completion_tokens": 16,
  "total_tokens": 24,
  "prompt_tokens_details": {
    "cached_tokens": 0
  },
  "completion_tokens_details": {
    "reasoning_tokens": 16
  },
  "prompt_cache_hit_tokens": 0,
  "prompt_cache_miss_tokens": 8
}
```

LongCat `LongCat-2.0` 返回：

```json
{
  "effectiveCachedTokens": 0,
  "completion_tokens": 16,
  "prompt_tokens": 11,
  "total_tokens": 27,
  "prompt_tokens_details": {
    "cached_tokens": 0,
    "audio_tokens": 0,
    "image_tokens": 0,
    "video_tokens": 0,
    "text_tokens": 0
  },
  "cache_write_tokens": 0,
  "cache_read_tokens": 0,
  "input_tokens": 0,
  "output_tokens": 0,
  "output_tokens_details": null,
  "cached_tokens": 0
}
```

两家都提供有效的：

```text
prompt_tokens
completion_tokens
total_tokens
```

LongCat 同时返回的 `input_tokens/output_tokens` 为兼容占位值 `0`，不能作为
主要统计来源。

### 2.6 用户确认的最终约束

1. 当前处于测试阶段，可以随时删除旧数据并破坏性修改 Schema。
2. 不考虑旧 SQLite、旧 Event、旧 Msg、旧 JSON 会话的兼容。
3. 不编写迁移逻辑、兼容读取分支或双写逻辑。
4. 暂时只支持能够返回以下三个字段的 OpenAI-compatible LLM：

   ```text
   prompt_tokens
   completion_tokens
   total_tokens
   ```

5. 暂时不设计缓存 Token、推理 Token、音频、图像或供应商私有字段。
6. Token 信息放入 Msg 顶层的 `token` 命名空间。
7. 累计消费与最后一次调用必须分开。

---

## 3. 问题根因

### 3.1 当前 Core 行为

当前 `ftre-agent-core` 中：

- `MODEL_CALL_END` 使用 `input_tokens/output_tokens`；
- 一个 Reply 内所有 `MODEL_CALL_END` 被聚合成一个 `Msg`；
- `Msg.usage` 对每次模型调用进行累加。

因此当前 `Msg.usage` 的真实语义是：

> 一个 assistant Reply 内所有 LLM Call 的累计 Token 消费。

该语义本身可用于成本统计，没有问题。

### 3.2 当前后端误用

Gateway 的 `SessionManager._find_anchor()` 倒序找到最新带 usage 的 Msg，
然后把 `Msg.usage` 当成“最近一次 LLM 调用”。

这在单次 Reply 只有一次 LLM Call 时看不出问题；只要 ReAct 回合执行多个
工具并调用多次模型，就会把每次逐渐增长的 prompt 重复相加。

### 3.3 当前客户端放大问题

Desktop：

- 直接信任 `/token_usage` 返回的 `total`；
- 还保留 `message.usage`、相邻消息差值 `turnUsage`、
  `TURN_END.token_usage` 三套展示来源；
- 百分比使用 `Math.min(..., 100)`，导致超过窗口时仍只显示 `100%`。

客户端不是一百万数值的产生者，但旧的多来源逻辑增加了语义混乱。

### 3.4 测试缺口

现有后端测试只构造一条带单次 usage 的 assistant Msg，没有覆盖：

```text
一个 assistant Reply
  ├── MODEL_CALL_END #1
  ├── 工具调用
  ├── MODEL_CALL_END #2
  ├── 工具调用
  └── MODEL_CALL_END #3
```

因此“累计 usage 被当成最后一次调用”的回归没有被发现。

---

## 4. 设计目标

### 4.1 必须实现

- 全链路统一 OpenAI Token 字段命名；
- 保留 assistant Reply 的累计消费；
- 保留最后一次 LLM Call 的精确 usage；
- 上下文水位只使用最后一次调用作为真实锚点；
- 最后一次调用之后尚未进入模型的消息使用字符估算；
- 自动压缩与 Desktop TokenRing 使用同一个权威算法；
- 历史加载与流式展示得到完全一致的 Msg 结构；
- 删除客户端旧 usage 兼容逻辑；
- 删除后端旧 Event/Msg usage 兼容逻辑。

### 4.2 非目标

本轮不实现：

- Anthropic、Gemini 等原生 usage 协议；
- `cached_tokens`；
- `reasoning_tokens`；
- `cache_read_tokens/cache_write_tokens`；
- 音频、图像、视频细分 Token；
- 每次 LLM Call 明细在 Msg 内长期持久化；
- 对旧 session JSON 的迁移；
- 对旧 `input_tokens/output_tokens` 的兼容读取；
- 跨模型 tokenizer 的精确本地计数。

每次 LLM Call 的完整原始 usage 如需诊断，继续由 Trace 负责。

---

## 5. 最终数据模型

### 5.1 TokenUsage

```python
class TokenUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
```

约束：

- 三个字段均为非负整数；
- 对受支持的 Provider，成功的模型调用应提供全部三个字段；
- 不引入 `input_tokens/output_tokens` 别名；
- 不引入供应商原始字段容器；
- 不引入可选缓存或推理字段。

### 5.2 MsgToken

```python
class MsgToken(BaseModel):
    # 当前 assistant Reply 内所有 LLM Call 累计
    usage: TokenUsage

    # 当前 assistant Reply 的最后一次 LLM Call
    last_call_usage: TokenUsage
```

### 5.3 Msg

```python
class Msg(BaseModel):
    name: str
    content: list[ContentBlock]
    role: Literal["user", "assistant", "system"]
    id: str
    metadata: dict
    created_at: str

    token: MsgToken | None = None

    finished_at: str | None = None
    finished_reason: ReplyFinishedReason | None = None
    structured_output: dict | None = None
    error: dict[str, Any] | None = None
```

角色约束：

- 只有 `role == "assistant"` 的 Msg 允许存在 `token`；
- user/system Msg 的 `token` 必须为 `None`；
- JSON 序列化时建议使用 `exclude_none=True`，user/system 消息不输出
  `"token": null`。

### 5.4 持久化示例

```json
{
  "name": "tencent/glm-5.2",
  "role": "assistant",
  "id": "reply_abcd1234",
  "content": [
    {
      "type": "text",
      "text": "任务已经完成。"
    }
  ],
  "token": {
    "usage": {
      "prompt_tokens": 53027,
      "completion_tokens": 548,
      "total_tokens": 53575
    },
    "last_call_usage": {
      "prompt_tokens": 17919,
      "completion_tokens": 186,
      "total_tokens": 18105
    }
  },
  "metadata": {},
  "created_at": "2026-07-28T16:02:52+08:00",
  "finished_at": "2026-07-28T16:02:53+08:00",
  "finished_reason": "completed"
}
```

---

## 6. 核心语义与不变量

### 6.1 `token.usage`

语义：

> 一个 assistant Reply 从开始到结束期间，所有成功返回 usage 的 LLM Call
> 的累计消费。

用途：

- Assistant 消息上的“本轮 Token”展示；
- 成本统计；
- 调试单轮总消耗；
- `TURN_END` 汇总（如保留）。

禁止用途：

- 当前上下文水位；
- 自动压缩阈值；
- 模型窗口占用比例。

### 6.2 `token.last_call_usage`

语义：

> 当前 assistant Reply 中最后一个成功的 LLM Call 返回的 usage。

用途：

- 当前上下文真实锚点；
- 自动压缩判断；
- Desktop TokenRing；
- 最后一次调用的 prompt/output 展示。

更新规则：

- 每次 `MODEL_CALL_END` 直接覆盖；
- 不进行累加；
- 如果一次调用没有合法 usage，不覆盖此前有效值。

### 6.3 为什么最后一次调用可以作为上下文锚点

在 ReAct 回合中，后一次模型调用的 prompt 已包含：

- 之前的对话上下文；
- 当前 Reply 中此前的 assistant 输出；
- 工具调用；
- 工具结果；
- Hook 注入；
- 系统提示词和工具 Schema。

因此最后一次模型调用的：

```text
prompt_tokens + completion_tokens
```

近似等于该 Reply 结束时已进入模型并形成的新上下文。

如果 Reply 结束后又新增 user Msg、外部消息或其他尚未进入模型的 Msg，则对
这些 pending Msg 追加字符估算。

### 6.4 第一次与后续 MODEL_CALL_END

第一次：

```python
current = TokenUsage(
    prompt_tokens=event.prompt_tokens,
    completion_tokens=event.completion_tokens,
    total_tokens=event.total_tokens,
)

msg.token = MsgToken(
    usage=current.model_copy(deep=True),
    last_call_usage=current.model_copy(deep=True),
)
```

后续调用：

```python
msg.token.usage.prompt_tokens += event.prompt_tokens
msg.token.usage.completion_tokens += event.completion_tokens
msg.token.usage.total_tokens += event.total_tokens

msg.token.last_call_usage = current.model_copy(deep=True)
```

### 6.5 示例

一个 Reply 有三次调用：

```text
Call 1: 10,000 + 200 = 10,200
Call 2: 12,000 + 100 = 12,100
Call 3: 15,000 + 300 = 15,300
```

最终：

```json
{
  "token": {
    "usage": {
      "prompt_tokens": 37000,
      "completion_tokens": 600,
      "total_tokens": 37600
    },
    "last_call_usage": {
      "prompt_tokens": 15000,
      "completion_tokens": 300,
      "total_tokens": 15300
    }
  }
}
```

成本为 `37,600`，上下文锚点为 `15,300`。

---

## 7. Core 事件协议

### 7.1 MODEL_CALL_END

删除：

```python
input_tokens: int
output_tokens: int
cached_tokens: int
reasoning_tokens: int
```

改为：

```python
class ModelCallEndEvent(AgentStreamEvent):
    type: Literal["MODEL_CALL_END"] = "MODEL_CALL_END"
    reply_id: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    finished_reason: str | None = None
```

事件 JSON：

```json
{
  "type": "MODEL_CALL_END",
  "reply_id": "reply_abcd1234",
  "prompt_tokens": 17919,
  "completion_tokens": 186,
  "total_tokens": 18105,
  "finished_reason": "stop"
}
```

### 7.2 Provider usage 读取

当前只接受 OpenAI-compatible usage：

```python
usage["prompt_tokens"]
usage["completion_tokens"]
usage["total_tokens"]
```

不再做：

```text
prompt_tokens → input_tokens → prompt_tokens
completion_tokens → output_tokens → completion_tokens
```

当一次成功调用缺失任一字段时：

- 记录 warning，包含 provider/model，但不得包含 API key；
- 不生成有效 TokenUsage；
- 不用零值覆盖已有 `last_call_usage`；
- Reply 内容仍按原有错误处理策略执行。

本轮不为非 OpenAI 原生协议增加映射。

### 7.3 RunnerState

如果继续保留 `RunnerState.token_usage`，其结构也统一为：

```python
{
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
}
```

删除：

```text
cached_tokens
llm_calls
```

该字段表示当前 Agent Run 的累计消费，与 `Msg.token.usage` 数值应一致。

`TURN_END.token_usage` 可以暂时保留为运行状态事件，但 Desktop 不再把它
作为历史消息 Token 的第二数据源。

---

## 8. Gateway 持久化与 HTTP 协议

### 8.1 state.json

`AgentStateFile.messages[]` 直接保存新的 Msg：

```json
{
  "role": "assistant",
  "token": {
    "usage": {
      "prompt_tokens": 53027,
      "completion_tokens": 548,
      "total_tokens": 53575
    },
    "last_call_usage": {
      "prompt_tokens": 17919,
      "completion_tokens": 186,
      "total_tokens": 18105
    }
  }
}
```

删除旧字段：

```json
{
  "usage": {
    "input_tokens": 53027,
    "output_tokens": 548
  }
}
```

不兼容读取旧结构。

### 8.2 Session Message API

Desktop 历史接口返回的 Message 与持久化 Msg 保持同构：

```typescript
interface SessionMessage {
  // existing fields...
  token: MessageToken | null;
}
```

不得在 API 路由或 Desktop session store 中进行字段重命名。

### 8.3 上下文 Token Usage API

保留接口：

```text
GET /api/sessions/{session_id}/token_usage
```

响应改为：

```json
{
  "session_id": "ws_sess_abcd1234",
  "last_call_usage": {
    "prompt_tokens": 17919,
    "completion_tokens": 186,
    "total_tokens": 18105
  },
  "pending_estimated": 0,
  "total": 18105
}
```

删除旧命名：

```text
anchor
anchor.source
anchor.at
```

`last_call_usage` 已明确表达数据来源，无需保留旧 Event 时代的
`MODEL_CALL_END/msg` source。

### 8.4 上下文计算算法

伪代码：

```python
async def get_token_usage(session_id: str) -> dict:
    messages = await get_context_messages(session_id)

    anchor_index = -1
    last_call_usage = None

    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        token = msg.get("token")
        if msg["role"] == "assistant" and token:
            last_call_usage = token.get("last_call_usage")
            if last_call_usage:
                anchor_index = index
                break

    pending = (
        messages[anchor_index + 1:]
        if anchor_index >= 0
        else messages
    )
    pending_estimated = estimate_messages_tokens(pending)

    if last_call_usage is None:
        total = pending_estimated
    else:
        total = (
            last_call_usage["total_tokens"]
            + pending_estimated
        )

    return {
        "session_id": session_id,
        "last_call_usage": last_call_usage,
        "pending_estimated": pending_estimated,
        "total": total,
    }
```

必须基于 `get_context_messages()`，即：

```text
current summary + through_message_id 之后的 tail
```

禁止基于完整 transcript 计算，否则 compact 后水位不会下降。

### 8.5 无锚点行为

以下情况可能没有 `last_call_usage`：

- 全新会话；
- 只有 user Msg；
- 模型调用失败且没有 usage；
- 当前 summary 后尚无成功 assistant Reply。

此时：

```text
last_call_usage = null
pending_estimated = 对当前 summary + tail 的全量估算
total = pending_estimated
```

这不是旧数据兼容，而是新会话的正常运行路径。

---

## 9. Desktop 客户端设计

### 9.1 TypeScript 类型

```typescript
export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface MessageToken {
  usage: TokenUsage;
  last_call_usage: TokenUsage;
}

export interface ChatMessage {
  // existing fields...
  token?: MessageToken;
}

export interface ContextTokenUsage {
  last_call_usage: TokenUsage | null;
  pending_estimated: number;
  total: number;
}
```

删除：

```text
ChatMessage.usage
ChatMessage.turnUsage
turnAccumulatedUsage
cached_tokens
reasoning_tokens
llm_calls
```

### 9.2 历史消息恢复

`SessionMessage` 直接映射：

```typescript
token: record.token ?? undefined
```

删除：

```typescript
prompt_tokens: record.usage.input_tokens
completion_tokens: record.usage.output_tokens
```

历史加载与实时事件最终必须生成相同的 `ChatMessage.token`。

### 9.3 流式 MODEL_CALL_END

```typescript
const current: TokenUsage = {
  prompt_tokens: data.prompt_tokens,
  completion_tokens: data.completion_tokens,
  total_tokens: data.total_tokens,
};

const previous = message.token?.usage;

message.token = {
  usage: previous
    ? {
        prompt_tokens:
          previous.prompt_tokens + current.prompt_tokens,
        completion_tokens:
          previous.completion_tokens + current.completion_tokens,
        total_tokens:
          previous.total_tokens + current.total_tokens,
      }
    : { ...current },
  last_call_usage: { ...current },
};
```

必须验证同一个 Reply 连续收到多个事件时：

- `usage` 持续累计；
- `last_call_usage` 始终等于最后一个事件；
- 切换 session 不串数据；
- 重载历史后数值不变化。

### 9.4 Assistant 消息展示

Assistant 消息的 Token 按钮只读取：

```typescript
message.token?.usage
```

展示：

```text
本轮       53.6K
输入       53,027
输出          548
合计       53,575
```

删除以下旧逻辑：

- 相邻 assistant message usage 做差；
- `turnUsage ?? message.usage`；
- `turnAccumulatedUsage ?? turnUsage ?? message.usage`；
- cached/reasoning/llm_calls 表格；
- 使用 `TURN_END.token_usage` 修补历史消息。

### 9.5 TokenRing

数据来源：

```typescript
const actual =
  usage.last_call_usage?.total_tokens ?? 0;
const estimated = usage.pending_estimated;
const total = usage.total;
```

百分比：

```typescript
const rawPct = contextWindow
  ? (total / contextWindow) * 100
  : 0;

const ringPct = Math.min(rawPct, 100);
```

规则：

- 圆环绘制可以限制为 `100%`；
- 数字标签和 Tooltip 必须显示 `rawPct`；
- 真实 `110.4%` 不得显示成 `100.0%`；
- 小于 `0.1%` 可以继续显示 `< 0.1%`。

Tooltip：

```text
上下文用量
最后一次调用       18,105
未实算部分            ≈ 0
合计                 18,105
上下文窗口        1,000,000
占比                   1.8%
```

### 9.6 Store 命名

建议将当前容易混淆的：

```text
tokenUsage
contextTokens
```

整理为：

```text
contextTokenUsage: ContextTokenUsage | null
contextWindow: number | null
```

如果本轮不希望扩大改动范围，可以暂时保留 store 属性名 `tokenUsage`，但其
TypeScript 类型和数据结构必须更新。禁止保留 `contextTokens` 的 deprecated
双写。

---

## 10. 多仓库实施计划

实施顺序必须是：

```text
ftre-agent-core
  → ftre Gateway
    → ftre-desktop
      → 删除旧 session 数据
        → 联调与验收
```

不能先改 Desktop 再等待后端协议跟进。

### Phase 0：建立基线

仓库：全部

任务：

1. 记录三个仓库 `git status --short`；
2. 保留用户已有改动，不覆盖无关文件；
3. 运行当前相关测试，记录已知失败；
4. 确认 Core 被 ftre 以 editable/local 方式引用；
5. 确认 Gateway 和 Desktop 当前均关闭，避免删除 session 后运行进程重新写入。

注意：

- `ftre-desktop` 当前已有未提交修改；
- 不得覆盖这些修改；
- 不得自行 commit 或 push。

### Phase 1：修改 ftre-agent-core

仓库：

```text
E:\ftre-agent-core
```

主要文件：

```text
src/ftre_agent_core/event/_event.py
src/ftre_agent_core/message/_msg.py
src/ftre_agent_core/agent/runner/_execute_reasoning.py
src/ftre_agent_core/agent/runner/_state.py
src/ftre_agent_core/hooks.py
tests/
```

任务：

1. 将 `Usage` 改为 `TokenUsage`；
2. 字段改为 `prompt_tokens/completion_tokens/total_tokens`；
3. 新增 `MsgToken`；
4. 将 `Msg.usage` 替换为 `Msg.token`；
5. 增加 assistant-only 校验；
6. 修改 `MODEL_CALL_END` 事件字段；
7. Runner 从 Provider usage 读取三个 OpenAI 字段；
8. Msg 聚合时累计 `token.usage`；
9. Msg 聚合时覆盖 `token.last_call_usage`；
10. 简化 `RunnerState.token_usage`；
11. 更新 Hook 类型和文档；
12. 删除缓存、推理和 `input/output` 旧字段。

Core 必须新增的测试：

1. 单次 `MODEL_CALL_END` 初始化两个 usage；
2. 三次 `MODEL_CALL_END` 累计正确；
3. `last_call_usage` 等于第三次调用；
4. user/system Msg 携带 token 时校验失败；
5. JSON round-trip 不丢字段；
6. 无 usage 的失败调用不覆盖最后有效 usage；
7. RunnerState 每次 run 开始时三个字段归零；
8. Event 序列化只包含新字段；
9. 全仓库不存在生产代码 `input_tokens/output_tokens` Token 事件字段。

### Phase 2：修改 ftre Gateway

仓库：

```text
E:\ftre
```

主要文件：

```text
src/ftre/session/manager.py
src/ftre/session/state.py
src/ftre/session/converter.py
src/ftre/session/token_counter.py
src/ftre/agent/turn_executor.py
src/ftre/agent/compact_manager.py
src/ftre/api/routes.py
tests/test_session_manager_baseline.py
tests/test_session_context_messages.py
tests/test_event_stream_history.py
tests/test_compact_summary.py
```

任务：

1. Session Message 模型输出 `token`；
2. 删除旧 `usage` 字段映射；
3. EventBus/WebSocket 输出新版 `MODEL_CALL_END`；
4. `_find_anchor()` 攅读 `token.last_call_usage`；
5. `/token_usage` 删除 `anchor`，返回 `last_call_usage`；
6. `pending_estimated` 继续只估算锚点之后的 Msg；
7. compact 阈值继续使用 `get_token_usage()["total"]`；
8. compact metadata 中 `tokens_before/trigger_ratio` 使用修正后的水位；
9. `TURN_END.token_usage` 如保留，使用三字段结构；
10. 删除所有旧 Event/Msg usage 兼容分支；
11. 更新 API 注释和设计文档中的旧 Schema 示例。

Gateway 必须新增的测试：

1. 一个 assistant Msg 内三次调用，API 只用最后一次作为锚点；
2. `usage=37,600`、`last_call_usage=15,300` 时上下文基数为
   `15,300`；
3. 锚点后新增 user Msg 时只追加该 user Msg 估算；
4. compact 后只统计 summary + tail；
5. 无锚点时全量估算；
6. summary 后第一个新 assistant 的最后调用成为新锚点；
7. API JSON 不含 `anchor/source/at`；
8. Session 历史 JSON 不含旧 `usage.input_tokens`；
9. 自动压缩不会因 Reply 累计消费超过窗口而误触发；
10. `state.json` round-trip 保持 `token` 结构。

### Phase 3：修改 ftre-desktop

仓库：

```text
E:\binn\ftre-desktop
```

主要文件：

```text
packages/renderer/src/services/api.ts
packages/renderer/src/stores/chat.ts
packages/renderer/src/stores/session.ts
packages/renderer/src/plugins/builtin/chat/AssistantMessage.tsx
packages/renderer/src/plugins/builtin/chat/ChatMessageList.tsx
packages/renderer/src/plugins/builtin/chat/TokenRing.tsx
packages/renderer/src/stores/chat.protocol.test.ts
packages/renderer/src/stores/session.test.ts
```

任务：

1. 新增 `TokenUsage/MessageToken/ContextTokenUsage` 类型；
2. `ChatMessage` 改为 `token?: MessageToken`；
3. Session 历史直接读取 `record.token`；
4. 流式事件使用新字段；
5. 累计 `token.usage`；
6. 覆盖 `token.last_call_usage`；
7. Assistant 消息只展示 `token.usage`；
8. 删除相邻消息做差；
9. 删除 `turnUsage/turnAccumulatedUsage`；
10. 删除 cached/reasoning/llm_calls UI；
11. TokenRing 读取新版上下文接口；
12. 百分比文字不截断到 `100%`；
13. 删除旧协议兼容代码和测试 fixture。

Desktop 必须新增的测试：

1. 连续三个 `MODEL_CALL_END` 后累计与最后一次值正确；
2. Session 历史加载得到相同结构；
3. Assistant 消息展示累计消费；
4. TokenRing 展示最后调用 + pending；
5. `total > contextWindow` 时文字显示超过 `100%`；
6. 切换 session 后旧请求响应不会污染新 session；
7. user/system 消息不显示 Token 按钮；
8. 全仓库不再引用 `turnAccumulatedUsage`；
9. 全仓库不再读取 `record.usage.input_tokens`。

### Phase 4：删除旧数据

用户已明确授权测试阶段直接删除旧 session 数据，不进行迁移。

目标：

```text
C:\Users\蒋全明\.ftre\sessions
```

执行前：

1. 确认 Gateway 已关闭；
2. 确认 Desktop 已关闭；
3. 再次解析目标绝对路径；
4. 确认目标父目录严格为 `C:\Users\蒋全明\.ftre`；
5. 禁止扩大到整个 `.ftre` 配置目录。

删除后：

- Gateway 首次创建 session 时自动重建 `sessions`；
- 不恢复旧数据；
- 不运行迁移；
- 不保留旧 session 备份；
- 报告实际删除结果。

注意：如果执行环境的安全策略拒绝递归删除，应明确报告，不得声称已经删除，
也不得改用不安全的跨 Shell 删除方式。

### Phase 5：联调

1. 启动 Gateway：

   ```text
   ftre gateway
   ```

2. 启动 Desktop：

   ```text
   cd E:\binn\ftre-desktop
   pnpm dev
   ```

3. 新建会话；
4. 发起一个不使用工具的简单请求；
5. 确认：

   ```text
   token.usage == token.last_call_usage
   ```

6. 发起一个至少执行三次 LLM Call 的工具任务；
7. 确认：

   ```text
   token.usage.total_tokens
     > token.last_call_usage.total_tokens
   ```

8. 确认 Assistant 消息展示累计消费；
9. 确认 TokenRing 展示最后一次调用；
10. 读取新 `state.json` 确认 Schema；
11. 重启 Gateway/Desktop；
12. 确认历史加载后的数值与实时阶段一致；
13. 手动 compact；
14. 确认 TokenRing 明显下降；
15. 验证自动 compact 不会因累计消费误触发。

---

## 11. 验证命令

### 11.1 Core

```powershell
python -m pytest -q
```

至少应单独运行：

```powershell
python -m pytest tests/test_message.py tests/test_state.py -q
```

实际测试文件名以仓库现状为准。

### 11.2 Gateway

```powershell
python -m pytest `
  tests/test_session_manager_baseline.py `
  tests/test_session_context_messages.py `
  tests/test_event_stream_history.py `
  tests/test_compact_summary.py `
  -q
```

然后运行：

```powershell
python -m pytest -q
```

### 11.3 Desktop

```powershell
pnpm test
pnpm build
```

如果全量测试存在既有失败，必须：

- 单独运行本次相关测试；
- 记录全量失败数量；
- 区分既有失败与本次回归；
- 不为了让测试变绿而修改无关测试。

### 11.4 静态残留检查

Core：

```powershell
rg -n "input_tokens|output_tokens|cached_tokens|reasoning_tokens" `
  src tests
```

Gateway：

```powershell
rg -n "input_tokens|output_tokens|anchor|MODEL_CALL_END" `
  src tests
```

Desktop：

```powershell
rg -n "turnAccumulatedUsage|turnUsage|input_tokens|output_tokens|cached_tokens|reasoning_tokens" `
  packages/renderer/src
```

每个命中都必须人工判断。与非 Token 协议相关的通用 `input/output` 文本不属于
清理目标。

---

## 12. 验收标准

全部满足才算完成：

### 数据结构

- [ ] `Msg` 顶层只有一个 Token 入口：`token`；
- [ ] `token.usage` 表示 Reply 累计；
- [ ] `token.last_call_usage` 表示最后一次调用；
- [ ] 三个 Token 字段统一为 OpenAI 命名；
- [ ] user/system Msg 不持有 token；
- [ ] 新 `state.json` 不含旧 usage 结构。

### Core

- [ ] 多次 `MODEL_CALL_END` 聚合正确；
- [ ] 最后一次调用覆盖正确；
- [ ] Event 不再发送 `input_tokens/output_tokens`；
- [ ] RunnerState 不再携带缓存、推理和 llm_calls。

### Gateway

- [ ] `/token_usage` 不再使用累计 `Msg.usage`；
- [ ] 接口返回 `last_call_usage`；
- [ ] compact 使用修正后的 total；
- [ ] 工具密集 Reply 不再虚增上下文；
- [ ] compact 后水位下降。

### Desktop

- [ ] Assistant 消息显示 Reply 累计消费；
- [ ] TokenRing 显示最后调用 + pending；
- [ ] 不再存在三套 usage 数据源；
- [ ] 超过窗口时文字百分比不截断；
- [ ] 历史恢复与实时显示一致。

### 数据与兼容

- [ ] 旧 sessions 已按授权删除；
- [ ] 没有迁移代码；
- [ ] 没有旧 Schema fallback；
- [ ] 没有双写；
- [ ] 没有修改或删除 `.ftre` 下其他配置。

### 质量

- [ ] Core 相关测试通过；
- [ ] Gateway 相关测试通过；
- [ ] Desktop 相关测试通过；
- [ ] Desktop build 通过；
- [ ] Gateway + Desktop 实际联调通过；
- [ ] 未提交、未 push，除非用户另行明确要求。

---

## 13. 风险与处理

### 13.1 Provider 成功响应不含 usage

本设计只支持提供三个 OpenAI usage 字段的 Provider。

处理：

- 记录 warning；
- 不覆盖最后有效 usage；
- 当前上下文退化为已有锚点 + pending，或全量估算；
- 不增加其他厂商协议兼容。

### 13.2 流式 usage 只在最后 chunk 返回

模型适配层必须保存流中最后一个非空 usage，并在模型调用结束时只发一个
`MODEL_CALL_END`。

禁止对每个 stream chunk 重复累计同一调用。

### 13.3 Reply 取消或异常

已完成的 LLM Call usage 可以保留；未完成且未返回 usage 的调用不得伪造
零值 `MODEL_CALL_END`。

### 13.4 模型切换

Token 数值使用最后一次 Provider 上报值；窗口大小使用 Desktop 当前选中模型的
`context_window`。

模型切换后尚未发生新调用时，百分比只是上一模型 Token 数在新窗口下的近似
展示。下一次成功调用会刷新锚点。本轮不为不同 tokenizer 增加转换。

### 13.5 字符估算误差

字符估算只用于 pending Msg 或没有任何真实锚点的新会话。

不得用全量字符估算覆盖有效 `last_call_usage`。

---

## 14. 建议提交拆分

只有用户明确要求提交时才执行。

建议按仓库分别提交：

### ftre-agent-core

```text
refactor!: unify token usage protocol and track last model call
```

### ftre

```text
refactor!: use last-call token usage for context accounting
```

### ftre-desktop

```text
refactor!: align message token model and context usage UI
```

每个仓库独立 commit、独立验证。Core commit 后先验证 Gateway，再处理 Desktop。

---

## 15. 最终架构摘要

```text
OpenAI-compatible LLM response
  usage:
    prompt_tokens
    completion_tokens
    total_tokens
          │
          ▼
MODEL_CALL_END
  prompt_tokens
  completion_tokens
  total_tokens
          │
          ▼
Assistant Msg.token
  ├── usage
  │     Reply 内所有 LLM Call 累计
  │     用于成本与 Assistant 消息展示
  │
  └── last_call_usage
        最后一次 LLM Call
        用于上下文锚点与自动压缩
          │
          ▼
Gateway /token_usage
  last_call_usage + pending_estimated
          │
          ▼
Desktop TokenRing
```

一句话定义：

> `token.usage` 回答“这个 Assistant Reply 一共花了多少 Token”，
> `token.last_call_usage` 回答“最后一次模型调用实际占用了多少上下文”。
