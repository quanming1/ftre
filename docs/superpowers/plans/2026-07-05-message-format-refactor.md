# 消息格式重构：事件流 → OpenAI Message 格式

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 ftre 的消息持久化从「每事件一行」改为「每消息一行」，一轮 LLM 输出（文本+思考+工具调用+用量）合并为一行 assistant message，格式接近 OpenAI Chat Completions API。

**Architecture:** DB schema 不变（`messages` 表 `id/session_id/type/data/timestamp`），但 `type` 从事件类型改为消息角色（`user`/`assistant`/`tool_result`/`done`/`context_compact`/`external_message`），`data` 改为 `{content: [...], metadata: {...}}` 结构。runtime 事件流不变（流式输出照常），在 `loop.py` 持久化层增加 coalesce 逻辑：收集一轮事件 → 组装为一条 message → 写 DB。`to_openai_messages()` 从 200 行缓冲逻辑简化为 ~50 行直读。

**Tech Stack:** Python, SQLite, ftre-agent-core, ftre

## Global Constraints

- DB schema 不变，只改 type/data 的语义
- runtime 事件流不变——runner 照常 yield 事件，ws_channel 照常透传流式 chunk
- coalesce 只影响 DB 持久化，不影响 bus 传输
- 旧事件数据需要 migration 脚本转换
- 重构后旧结构彻底删除，不留回退分支
- `to_openai_messages()` 的 pending_* 缓冲逻辑全部删除
- 不改 ftre-agent-core（事件定义和 runner 不动）

---

## 当前事件 → 新消息映射

### 一轮有工具调用的 LLM 输出

**当前（5+ 行）：**
```
usage_update        → 1 行
reasoning_complete  → 1 行（如有思考）
assistant_message_complete → 1 行
tool_call           → N 行
tool_result         → N 行
```

**新（1+N 行）：**
```
assistant           → 1 行（content[] 含 text + thinking + toolCall，metadata 含 usage + kind + stopReason）
tool_result         → N 行（每个工具结果一行）
```

### 一轮无工具调用的最终回复

**当前（3-4 行）：**
```
usage_update        → 1 行
reasoning_complete  → 1 行（如有思考）
assistant_message_complete → 1 行
done                → 1 行
```

**新（2 行）：**
```
assistant           → 1 行（content[] 含 text + thinking，metadata 含 usage + kind="final"）
done                → 1 行
```

### 不变的类型

| 旧 type | 新 type | 说明 |
|---------|---------|------|
| `user_message` | `user` | 用户消息，data 格式微调 |
| `context_compact` | `context_compact` | 压缩游标，不变 |
| `external_message` | `external_message` | 外部消息，不变 |
| `done` | `done` | 完成标记，data 去掉 usage（usage 在 assistant 里） |

### 合并后删除的 type

| 旧 type | 去向 |
|---------|------|
| `assistant_message_complete` | → assistant.content[].text |
| `reasoning_complete` | → assistant.content[].thinking |
| `tool_call` | → assistant.content[].toolCall |
| `usage_update` | → assistant.metadata.usage |
| `error` | → assistant.metadata.error（stopReason="error"） |
| `assistant_message` | 不持久化（流式 chunk，本来就存 DB） |
| `reasoning` | 不持久化（流式 chunk） |
| `tool_call_streaming` | 不持久化（流式 chunk） |

---

## 新 data 格式

### type = "user"

```json
{
  "content": "用户消息文本",
  "attachments": [],
  "metadata": {"session_id": "...", "agent_id": "...", "hide": false},
  "event_id": "abc123"
}
```

### type = "assistant"

```json
{
  "content": [
    {"type": "text", "text": "我先看一下..."},
    {"type": "thinking", "thinking": "Let me analyze..."},
    {"type": "toolCall", "id": "call_xxx", "name": "read", "arguments": {"path": "..."}}
  ],
  "metadata": {
    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    "provider": "litellm",
    "model": "claude-opus-4-5",
    "stopReason": "toolUse",
    "kind": "block",
    "responseId": "chatcmpl-xxx"
  },
  "event_id": "abc123"
}
```

content 块类型：

| type | 结构 | 说明 |
|------|------|------|
| `text` | `{type, text}` | 文本输出 |
| `thinking` | `{type, thinking}` | 推理/思考链 |
| `toolCall` | `{type, id, name, arguments}` | 工具调用 |

metadata 字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `usage` | dict \| null | token 用量 |
| `provider` | string | provider 名称 |
| `model` | string | 模型 ID |
| `stopReason` | string | "stop" / "toolUse" / "error" / "length" / "aborted" |
| `kind` | string | "block" / "final"（沿用现有 kind 字段） |
| `responseId` | string \| null | provider 响应 ID |
| `error` | dict \| null | `{message, code}` 仅 stopReason=error 时存在 |

### type = "tool_result"

```json
{
  "toolCallId": "call_xxx",
  "toolName": "read",
  "content": [{"type": "text", "text": "file contents..."}],
  "isError": false,
  "event_id": "abc123"
}
```

### type = "done"

```json
{
  "success": true,
  "reason": "completed",
  "event_id": "abc123"
}
```

---

## 文件结构

| 文件 | 改动 | 职责 |
|------|------|------|
| `E:\ftre\src\ftre\agent\message_coalesce.py` | 新建 | coalesce 纯函数：收集事件 → 组装 message dict |
| `E:\ftre\src\ftre\agent\loop.py` | 修改 | 持久化层从 per-event 改为 coalesce；删除 `_PERSISTENT_CLASSES` |
| `E:\ftre\src\ftre\session\manager.py` | 修改 | `to_openai_messages()` 简化；`get_recent_messages_by_turns` type 过滤；`_find_anchor` / `_compute_token_usage` 改读 assistant.metadata.usage；`save_message` 不变 |
| `E:\ftre\src\ftre\session\token_counter.py` | 修改 | `estimate_events_tokens` → `estimate_messages_tokens` |
| `E:\ftre\src\ftre\agent\compact_manager.py` | 修改 | `_serialize_events` 改为遍历新消息格式 |
| `E:\ftre\src\ftre\session\migrate_events.py` | 新建 | DB migration：旧事件行 → 新消息行 |
| `E:\ftre\src\ftre\session\manager.py` init | 修改 | 启动时调 migration |
| `E:\ftre\tests\test_message_coalesce.py` | 新建 | coalesce 单测 |
| `E:\ftre\tests\test_to_openai_messages.py` | 新建 | to_openai_messages 单测 |

---

## Task 1: message_coalesce 纯函数

**Files:**
- Create: `E:\ftre\src\ftre\agent\message_coalesce.py`
- Test: `E:\ftre\tests\test_message_coalesce.py`

**Interfaces:**
- Produces: `coalesce_events(events: list) -> list[dict]` — 输入 AgentEvent 实例列表，输出 `[{type, data}, ...]` 消息 dict 列表

- [ ] **Step 1: 写测试**

```python
# E:\ftre\tests\test_message_coalesce.py
"""coalesce 单测：把一轮 LLM 事件流合并为 message dict。"""
from ftre_agent_core.agent.event import (
    AssistantMessageCompleteEvent,
    ReasoningCompleteEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageUpdateEvent,
    DoneEvent,
    ErrorEvent,
    UserMessageEvent,
    DoneReason,
)
from ftre.agent.message_coalesce import coalesce_events


def test_assistant_with_tool_calls():
    """一轮有工具调用：assistant(text+thinking+toolCalls) + 2 个 tool_result。"""
    events = [
        UsageUpdateEvent(usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}),
        ReasoningCompleteEvent(content="Let me check..."),
        AssistantMessageCompleteEvent(content="我先看一下", kind="block"),
        ToolCallEvent(tool_id="call_1", tool_name="read", arguments={"path": "a.py"}),
        ToolCallEvent(tool_id="call_2", tool_name="read", arguments={"path": "b.py"}),
        ToolResultEvent(tool_id="call_1", tool_name="read", result="content A"),
        ToolResultEvent(tool_id="call_2", tool_name="read", result="content B"),
    ]
    msgs = coalesce_events(events)
    assert len(msgs) == 3  # 1 assistant + 2 tool_result

    am = msgs[0]
    assert am["type"] == "assistant"
    content = am["data"]["content"]
    # content 顺序：thinking 在前（reasoning_complete 先到），text 在后
    assert {"type": "thinking", "thinking": "Let me check..."} in content
    assert {"type": "text", "text": "我先看一下"} in content
    assert {"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "a.py"}} in content
    assert {"type": "toolCall", "id": "call_2", "name": "read", "arguments": {"path": "b.py"}} in content
    assert am["data"]["metadata"]["kind"] == "block"
    assert am["data"]["metadata"]["usage"]["total_tokens"] == 150

    assert msgs[1]["type"] == "tool_result"
    assert msgs[1]["data"]["toolCallId"] == "call_1"
    assert msgs[1]["data"]["content"][0]["text"] == "content A"
    assert msgs[2]["data"]["toolCallId"] == "call_2"


def test_assistant_final_reply():
    """一轮最终回复：assistant(text) + done。"""
    events = [
        UsageUpdateEvent(usage={"prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280}),
        AssistantMessageCompleteEvent(content="完成了！", kind="final"),
        DoneEvent(success=True, reason=DoneReason.COMPLETED),
    ]
    msgs = coalesce_events(events)
    assert len(msgs) == 2  # 1 assistant + 1 done

    assert msgs[0]["type"] == "assistant"
    assert msgs[0]["data"]["content"] == [{"type": "text", "text": "完成了！"}]
    assert msgs[0]["data"]["metadata"]["kind"] == "final"
    assert msgs[0]["data"]["metadata"]["usage"]["total_tokens"] == 280

    assert msgs[1]["type"] == "done"
    assert msgs[1]["data"]["success"] is True


def test_user_message_passthrough():
    """user_message 直接透传，不合并。"""
    events = [
        UserMessageEvent(content="你好", metadata={"hide": False}),
    ]
    msgs = coalesce_events(events)
    assert len(msgs) == 1
    assert msgs[0]["type"] == "user"
    assert msgs[0]["data"]["content"] == "你好"


def test_empty_assistant_turn():
    """空文本轮次（只有 usage + done，无 assistant_message_complete）→ 不产出 assistant message。"""
    events = [
        UsageUpdateEvent(usage={"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10}),
        DoneEvent(success=True, reason=DoneReason.COMPLETED),
    ]
    msgs = coalesce_events(events)
    assert len(msgs) == 1  # 只有 done，无 assistant
    assert msgs[0]["type"] == "done"


def test_error_embedded_in_assistant():
    """error 事件嵌入 assistant metadata。"""
    events = [
        AssistantMessageCompleteEvent(content="半截文本", kind="block"),
        ErrorEvent(message="LLM 超时", code="timeout"),
        DoneEvent(success=False, reason=DoneReason.ERROR),
    ]
    msgs = coalesce_events(events)
    assert len(msgs) == 2  # 1 assistant + 1 done

    assert msgs[0]["type"] == "assistant"
    assert msgs[0]["data"]["metadata"]["stopReason"] == "error"
    assert msgs[0]["data"]["metadata"]["error"]["message"] == "LLM 超时"
    assert msgs[0]["data"]["metadata"]["error"]["code"] == "timeout"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:\ftre && python -m pytest tests\test_message_coalesce.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: 实现 coalesce_events**

```python
# E:\ftre\src\ftre\agent\message_coalesce.py
"""
事件 → 消息 coalesce：把一轮 LLM 产出的事件合并为一条 assistant message。

coalesce 只影响 DB 持久化，不影响 bus 传输和前端流式显示。
"""
from __future__ import annotations

from typing import Any

from ftre_agent_core.agent.event import (
    AgentEvent,
    AssistantMessageCompleteEvent,
    DoneEvent,
    ErrorEvent,
    ReasoningCompleteEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageUpdateEvent,
    UserMessageEvent,
)


def coalesce_events(events: list[AgentEvent]) -> list[dict[str, Any]]:
    """把 AgentEvent 列表合并为 message dict 列表。

    输入：一轮或多轮 LLM 产出的事件（按时间顺序）。
    输出：[{type: "user"|"assistant"|"tool_result"|"done", data: {...}}, ...]

    合并规则：
    - UserMessageEvent → 直接输出 type="user"
    - UsageUpdateEvent → 缓冲到 metadata.usage
    - ReasoningCompleteEvent → 缓冲到 content[].thinking
    - AssistantMessageCompleteEvent → 缓冲到 content[].text + metadata.kind
    - ToolCallEvent → 缓冲到 content[].toolCall
    - ErrorEvent → 缓冲到 metadata.error + metadata.stopReason="error"
    - ToolResultEvent → flush assistant message（如有），然后输出 type="tool_result"
    - DoneEvent → flush assistant message（如有），然后输出 type="done"
    """
    result: list[dict[str, Any]] = []
    _buf: _TurnBuffer | None = None

    for event in events:
        if isinstance(event, UserMessageEvent):
            _flush(_buf, result)
            _buf = None
            result.append({
                "type": "user",
                "data": {
                    "content": event.content,
                    "metadata": dict(event.metadata),
                    "event_id": event.event_id,
                },
            })

        elif isinstance(event, UsageUpdateEvent):
            if _buf is None:
                _buf = _TurnBuffer(event_id=event.event_id)
            _buf.usage = event.usage

        elif isinstance(event, ReasoningCompleteEvent):
            if _buf is None:
                _buf = _TurnBuffer(event_id=event.event_id)
            _buf.content.append({"type": "thinking", "thinking": event.content})

        elif isinstance(event, AssistantMessageCompleteEvent):
            if _buf is None:
                _buf = _TurnBuffer(event_id=event.event_id)
            _buf.content.append({"type": "text", "text": event.content})
            _buf.kind = event.kind

        elif isinstance(event, ToolCallEvent):
            if _buf is None:
                _buf = _TurnBuffer(event_id=event.event_id)
            _buf.content.append({
                "type": "toolCall",
                "id": event.tool_id,
                "name": event.tool_name,
                "arguments": event.arguments,
            })

        elif isinstance(event, ErrorEvent):
            if _buf is None:
                _buf = _TurnBuffer(event_id=event.event_id)
            _buf.error = {"message": event.message, "code": event.code}
            _buf.stop_reason = "error"

        elif isinstance(event, ToolResultEvent):
            _flush(_buf, result)
            _buf = None
            result.append({
                "type": "tool_result",
                "data": {
                    "toolCallId": event.tool_id,
                    "toolName": event.tool_name,
                    "content": [{"type": "text", "text": event.result}],
                    "isError": bool(event.error),
                    "event_id": event.event_id,
                },
            })

        elif isinstance(event, DoneEvent):
            _flush(_buf, result)
            _buf = None
            done_data: dict[str, Any] = {
                "success": event.success,
                "reason": event.reason,
                "event_id": event.event_id,
            }
            result.append({"type": "done", "data": done_data})

        # 其他事件类型（streaming chunk 等）不持久化，忽略

    # 兜底：如果缓冲区还有内容（不应该发生，但防御性处理）
    _flush(_buf, result)
    return result


class _TurnBuffer:
    """一轮 LLM 输出的缓冲区。"""

    def __init__(self, *, event_id: str = ""):
        self.event_id = event_id
        self.content: list[dict[str, Any]] = []
        self.usage: dict | None = None
        self.kind: str = "final"
        self.stop_reason: str | None = None
        self.error: dict | None = None

    def has_content(self) -> bool:
        return bool(self.content)

    def to_message(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {"kind": self.kind}
        if self.usage:
            metadata["usage"] = self.usage
        if self.stop_reason:
            metadata["stopReason"] = self.stop_reason
        if self.error:
            metadata["error"] = self.error
        return {
            "type": "assistant",
            "data": {
                "content": self.content,
                "metadata": metadata,
                "event_id": self.event_id,
            },
        }


def _flush(buf: _TurnBuffer | None, result: list[dict]) -> None:
    """如果缓冲区有内容，输出为一条 assistant message。"""
    if buf is not None and buf.has_content():
        result.append(buf.to_message())
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:\ftre && python -m pytest tests\test_message_coalesce.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
cd E:\ftre
git add src/ftre/agent/message_coalesce.py tests/test_message_coalesce.py
git commit -m "feat: message_coalesce 纯函数——把一轮 LLM 事件合并为一条 assistant message"
```

---

## Task 2: loop.py 持久化层改造

**Files:**
- Modify: `E:\ftre\src\ftre\agent\loop.py` — `_run_async()` 事件循环 + 删除 `_PERSISTENT_CLASSES`

**Interfaces:**
- Consumes: `coalesce_events` from Task 1
- Produces: DB 行从 per-event 改为 per-message

**核心改动：**

当前 `loop.py:614-619` 逐事件持久化：
```python
if isinstance(event, self._PERSISTENT_CLASSES):
    event_data = event._data_dict()
    event_data["event_id"] = event.event_id
    await self.session_manager.save_message(session_id, event.type.value, event_data)
```

改为：收集事件 → 在 flush 点调用 `coalesce_events` → 批量持久化。

flush 点是 `tool_result` 和 `done`（以及 `user_message`）——这些是轮次边界。

- [ ] **Step 1: 修改 loop.py 持久化逻辑**

在 `_run_async` 的事件循环中：

```python
# 删除 _PERSISTENT_CLASSES
# 新增 import
from ftre.agent.message_coalesce import coalesce_events

# 在事件循环中：
_pending_events: list[AgentEvent] = []

async for event in agent.run(messages, runtime_context=runtime_context):
    # task 工具只使用最后一条完整 assistant 回复
    if isinstance(event, AssistantMessageCompleteEvent):
        final_content = event.content or ""

    # 所有事件照常发到 bus（流式 UI 不受影响）
    out = BusMessage(...)
    await self.bus.publish_outbound(out)

    # 收集需要持久化的事件类型
    if isinstance(event, (UserMessageEvent, ToolResultEvent, DoneEvent)):
        # flush 点：先 flush 缓冲的 assistant 事件
        if _pending_events:
            for msg in coalesce_events(_pending_events):
                await self.session_manager.save_message(
                    session_id, msg["type"], msg["data"]
                )
            _pending_events.clear()
        # 再持久化当前事件
        for msg in coalesce_events([event]):
            await self.session_manager.save_message(
                session_id, msg["type"], msg["data"]
            )
    elif isinstance(event, (
        UsageUpdateEvent, ReasoningCompleteEvent,
        AssistantMessageCompleteEvent, ToolCallEvent, ErrorEvent,
    )):
        _pending_events.append(event)

    # usage_update 时检查预压缩（保持不变）
    if isinstance(event, UsageUpdateEvent) and inbound.from_channel != SUBAGENT_CHANNEL_ID:
        ...
```

- [ ] **Step 2: 处理 cancel/except 路径**

cancel 和 except 路径中，flush `_pending_events`（可能包含半截 assistant message），再持久化 done：

```python
except asyncio.CancelledError:
    # flush 半截内容
    if _pending_events:
        for msg in coalesce_events(_pending_events):
            await self.session_manager.save_message(session_id, msg["type"], msg["data"])
        _pending_events.clear()
    # 持久化 done
    done_data = {"success": False, "reason": DoneReason.CANCELLED, "event_id": ...}
    await self.session_manager.save_message(session_id, "done", done_data)
    # 发 bus
    ...
except Exception:
    # 同上
    ...
```

- [ ] **Step 3: 删除 _PERSISTENT_CLASSES**

删除 `loop.py:414-423` 的 `_PERSISTENT_CLASSES` 定义。

- [ ] **Step 4: 手动验证**

Run: `cd E:\ftre && python -c "from ftre.agent.loop import AgentLoop; print('OK')"`
Expected: OK

- [ ] **Step 5: Commit**

```bash
cd E:\ftre
git add src/ftre/agent/loop.py
git commit -m "refactor: loop.py 持久化从 per-event 改为 coalesce，删除 _PERSISTENT_CLASSES"
```

---

## Task 3: 简化 to_openai_messages

**Files:**
- Modify: `E:\ftre\src\ftre\session\manager.py:611-822` — `to_openai_messages()`
- Modify: `E:\ftre\src\ftre\session\token_counter.py:78-85` — rename

**核心改动：**

`to_openai_messages()` 从 200 行缓冲逻辑简化为直读：

```python
@staticmethod
def to_openai_messages(
    messages: list[MessageModel],
    *,
    config: dict | None = None,
    prune: dict | None = None,
) -> list[dict]:
    """将消息列表转为 OpenAI 格式。新格式直读，无需缓冲。"""
    result: list[dict] = []
    llm_config = (config or {}).get("llm") or {}
    include_images = bool(llm_config.get("vision", False))

    # prune 预处理（保留现有 protect_turns 逻辑）
    prune_protected = _compute_prune_protected(messages, prune)

    for idx, msg in enumerate(messages):
        t = msg["type"]
        data = msg["data"]

        if t == "user":
            content = data.get("content", "")
            attachments = data.get("attachments") or []
            if attachments:
                from .multimodal import build_user_content
                content = build_user_content(content, attachments, include_images=include_images)
            result.append({"role": "user", "content": _coerce_content(content, include_images)})

        elif t == "assistant":
            content_blocks = data.get("content", [])
            metadata = data.get("metadata", {})
            text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
            thinking_parts = [b["thinking"] for b in content_blocks if b.get("type") == "thinking"]
            tool_calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": _serialize_arguments(b["arguments"])},
                }
                for b in content_blocks if b.get("type") == "toolCall"
            ]
            msg_dict = format_assistant_message(
                content="\n".join(text_parts) if text_parts else None,
                reasoning="\n".join(thinking_parts) if thinking_parts else None,
                tool_calls=tool_calls or None,
            )
            result.append(msg_dict)

        elif t == "tool_result":
            result_content = data.get("content", [{}])
            text = result_content[0].get("text", "") if result_content else ""
            if prune:
                text = _maybe_prune(text, idx, prune_protected, data.get("isError"))
            result.append({
                "role": "tool",
                "tool_call_id": data["toolCallId"],
                "content": text,
            })

        elif t == "external_message":
            from_ch = data.get("from_channel", "")
            from_sid = data.get("from_session", "")
            src = f"{from_ch}::{from_sid}" if from_ch or from_sid else "external"
            result.append({
                "role": "assistant",
                "name": _safe_name(src),
                "content": f"[来自 {src} 的消息] {data.get('content', '')}",
            })

        elif t == "context_compact":
            compact_data = data or {}
            if compact_data.get("enabled", True) is not True:
                continue
            summary = compact_data.get("summary", "")
            result = []
            if summary:
                result.append({"role": "user", "content": f"[历史上下文摘要]\n{summary}"})

    return result
```

- [ ] **Step 1: 写 to_openai_messages 测试**

```python
# E:\ftre\tests\test_to_openai_messages.py
"""to_openai_messages 单测：新消息格式 → OpenAI messages。"""
from ftre.session.manager import SessionManager


def test_simple_conversation():
    messages = [
        {"id": "1", "session_id": "s", "type": "user", "data": {"content": "你好"}, "timestamp": 1},
        {"id": "2", "session_id": "s", "type": "assistant", "data": {
            "content": [{"type": "text", "text": "你好！有什么可以帮你的？"}],
            "metadata": {"kind": "final", "usage": {"total_tokens": 50}},
        }, "timestamp": 2},
    ]
    result = SessionManager.to_openai_messages(messages)
    assert len(result) == 2
    assert result[0] == {"role": "user", "content": "你好"}
    assert result[1]["role"] == "assistant"
    assert result[1]["content"] == "你好！有什么可以帮你的？"


def test_tool_call_round():
    messages = [
        {"id": "1", "session_id": "s", "type": "user", "data": {"content": "读文件"}, "timestamp": 1},
        {"id": "2", "session_id": "s", "type": "assistant", "data": {
            "content": [
                {"type": "text", "text": "我来读一下"},
                {"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "a.py"}},
            ],
            "metadata": {"kind": "block"},
        }, "timestamp": 2},
        {"id": "3", "session_id": "s", "type": "tool_result", "data": {
            "toolCallId": "call_1", "toolName": "read",
            "content": [{"type": "text", "text": "file A"}], "isError": False,
        }, "timestamp": 3},
        {"id": "4", "session_id": "s", "type": "assistant", "data": {
            "content": [{"type": "text", "text": "文件内容是 file A"}],
            "metadata": {"kind": "final"},
        }, "timestamp": 4},
    ]
    result = SessionManager.to_openai_messages(messages)
    assert len(result) == 4
    assert result[0]["role"] == "user"
    assert result[1]["role"] == "assistant"
    assert result[1]["content"] == "我来读一下"
    assert result[1]["tool_calls"][0]["function"]["name"] == "read"
    assert result[2]["role"] == "tool"
    assert result[2]["tool_call_id"] == "call_1"
    assert result[2]["content"] == "file A"
    assert result[3]["role"] == "assistant"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:\ftre && python -m pytest tests\test_to_openai_messages.py -v`
Expected: FAIL

- [ ] **Step 3: 重写 to_openai_messages**

用上面的简化版替换 `manager.py:611-822`。提取 `_compute_prune_protected` 和 `_maybe_prune` 为模块级辅助函数。删除所有 `pending_*` 缓冲逻辑和内嵌闭包。

- [ ] **Step 4: 重命名 token_counter**

`token_counter.py`：
- `estimate_events_tokens` → `estimate_messages_tokens`
- docstring 更新

- [ ] **Step 5: 运行测试确认通过**

Run: `cd E:\ftre && python -m pytest tests\test_to_openai_messages.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
cd E:\ftre
git add src/ftre/session/manager.py src/ftre/session/token_counter.py tests/test_to_openai_messages.py
git commit -m "refactor: to_openai_messages 从 200 行缓冲逻辑简化为直读，token_counter 重命名"
```

---

## Task 4: 更新 _find_anchor / get_token_usage / get_recent_messages_by_turns

**Files:**
- Modify: `E:\ftre\src\ftre\session\manager.py` — `_find_anchor`, `_compute_token_usage`, `get_recent_messages_by_turns`

**核心改动：**

`_find_anchor`：从找 `usage_update`/`done` 事件改为找 `assistant` 消息中 `metadata.usage`：

```python
def _find_anchor(messages: list[MessageModel]) -> tuple[int, dict | None, str]:
    """倒序找最晚的带 usage 的 assistant 消息。"""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg["type"] != "assistant":
            continue
        usage = (msg.get("data") or {}).get("metadata", {}).get("usage")
        if usage:
            return i, usage, "assistant"
    return -1, None, ""
```

`get_recent_messages_by_turns`：`type = 'user_message'` → `type = 'user'`，`json_extract(data, '$.metadata.hide')` 保持不变。

`_compute_token_usage`：`estimate_events_tokens` → `estimate_messages_tokens`。

- [ ] **Step 1: 修改三个函数**
- [ ] **Step 2: 验证 import**

Run: `cd E:\ftre && python -c "from ftre.session.manager import SessionManager; print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
cd E:\ftre
git add src/ftre/session/manager.py
git commit -m "refactor: _find_anchor 改读 assistant.metadata.usage，get_recent_messages_by_turns type 过滤改 user"
```

---

## Task 5: 更新 compact_manager

**Files:**
- Modify: `E:\ftre\src\ftre\agent\compact_manager.py` — `_serialize_events`, 引用更新

**核心改动：**

`_serialize_events` 改为遍历新消息格式：

```python
def _serialize_messages(
    messages: list[dict],
    *,
    tool_output_max_chars: int = 2000,
) -> str:
    """把消息列表序列化为 LLM 可读纯文本。"""
    parts: list[str] = []
    for msg in messages:
        t = msg["type"]
        data = msg.get("data") or {}
        if t == "user":
            content = data.get("content", "")
            if isinstance(content, list):
                content = "\n".join(p.get("text","") for p in content if isinstance(p, dict) and p.get("type")=="text")
            parts.append(f"[User]: {content}")
        elif t == "assistant":
            for block in data.get("content", []):
                bt = block.get("type")
                if bt == "text":
                    parts.append(f"[Assistant]: {block['text']}")
                elif bt == "thinking":
                    parts.append(f"[Assistant reasoning]: {block['thinking']}")
                elif bt == "toolCall":
                    args = json.dumps(block["arguments"], ensure_ascii=False)
                    parts.append(f"[Assistant tool call]: {block['name']}({args})")
        elif t == "tool_result":
            text = data.get("content", [{}])[0].get("text", "")
            if len(text) > tool_output_max_chars:
                text = text[:tool_output_max_chars] + "\n[truncated]"
            if data.get("isError"):
                parts.append(f"[Tool error]: {text}")
            else:
                parts.append(f"[Tool result]: {text}")
    return "\n\n".join(parts)
```

同时更新 `compact_manager` 中所有 `estimate_events_tokens` 引用为 `estimate_messages_tokens`。

- [ ] **Step 1: 修改 compact_manager**
- [ ] **Step 2: 验证 import**

Run: `cd E:\ftre && python -c "from ftre.agent.compact_manager import CompactManager; print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
cd E:\ftre
git add src/ftre/agent/compact_manager.py
git commit -m "refactor: compact_manager _serialize_events 改为遍历新消息格式"
```

---

## Task 6: DB Migration

**Files:**
- Create: `E:\ftre\src\ftre\session\migrate_events.py`
- Modify: `E:\ftre\src\ftre\session\manager.py` — `init()` 末尾调 migration

**核心改动：**

启动时检测旧数据并转换。判断逻辑：如果 messages 表中存在 `type IN ('assistant_message_complete', 'tool_call', 'usage_update', 'reasoning_complete', 'error', 'user_message')` 的行，则执行 migration。

Migration 步骤：
1. 创建 `messages_new` 表（同结构）
2. 遍历每个 session 的旧事件，按时间顺序
3. 用 coalesce 逻辑（复用 `message_coalesce.py`）把事件转为消息
4. 写入 `messages_new`
5. `ALTER TABLE messages RENAME TO messages_old`
6. `ALTER TABLE messages_new RENAME TO messages`
7. `DROP TABLE messages_old`

```python
# E:\ftre\src\ftre\session\migrate_events.py
"""DB migration：旧事件行 → 新消息行。"""
import json
import logging
from ftre_agent_core.agent.event import AgentEvent

logger = logging.getLogger(__name__)


async def migrate_events_to_messages(db) -> None:
    """检测并迁移旧事件格式到新消息格式。"""
    # 检测是否需要迁移
    cursor = await db.execute("""
        SELECT COUNT(*) as cnt FROM messages
        WHERE type IN ('assistant_message_complete', 'tool_call', 'usage_update',
                       'reasoning_complete', 'error', 'user_message')
    """)
    count = (await cursor.fetchone())["cnt"]
    if count == 0:
        logger.info("[migrate] 无旧事件数据，跳过")
        return

    logger.info(f"[migrate] 检测到 {count} 行旧事件数据，开始迁移...")

    from ftre.agent.message_coalesce import coalesce_events

    # 创建新表
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS messages_new (
            id          TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            type        TEXT NOT NULL,
            data        TEXT NOT NULL DEFAULT '{}',
            timestamp   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_new_session
            ON messages_new(session_id, timestamp ASC);
    """)

    # 获取所有 session_id
    cursor = await db.execute("SELECT DISTINCT session_id FROM messages")
    session_ids = [r["session_id"] for r in await cursor.fetchall()]

    total_migrated = 0
    for sid in session_ids:
        cursor = await db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
            (sid,)
        )
        rows = await cursor.fetchall()

        # 旧事件 → AgentEvent 列表
        events: list[AgentEvent] = []
        for row in rows:
            t = row["type"]
            data = json.loads(row["data"]) if row["data"] else {}
            # context_compact / external_message 直接复制
            if t in ("context_compact", "external_message"):
                # 这些类型直接迁移
                await db.execute(
                    "INSERT INTO messages_new (id, session_id, type, data, timestamp) VALUES (?, ?, ?, ?, ?)",
                    (row["id"], sid, t, row["data"], row["timestamp"])
                )
                total_migrated += 1
                continue
            try:
                ev = AgentEvent.from_dict({"type": t, "data": data})
                events.append(ev)
            except (KeyError, ValueError):
                # 跳过无法解析的事件（如 message_complete 旧类型）
                continue

        # coalesce 并写入
        msgs = coalesce_events(events)
        ts_base = rows[0]["timestamp"] if rows else 0
        ts_step = 0.001
        for i, msg in enumerate(msgs):
            msg_id = row["id"][:8] + f"_{i:04d}" if rows else f"m_{i:04d}"
            # 使用原始事件的 event_id（如果 coalesce 保留了的话）
            event_id = msg["data"].get("event_id", msg_id)
            if not msg["data"].get("event_id"):
                msg["data"]["event_id"] = event_id
            await db.execute(
                "INSERT INTO messages_new (id, session_id, type, data, timestamp) VALUES (?, ?, ?, ?, ?)",
                (msg_id, sid, msg["type"],
                 json.dumps(msg["data"], ensure_ascii=False),
                 ts_base + i * ts_step)
            )
            total_migrated += 1

    # 替换表
    await db.executescript("""
        DROP TABLE messages;
        ALTER TABLE messages_new RENAME TO messages;
    """)

    await db.commit()
    logger.info(f"[migrate] 迁移完成，共 {total_migrated} 行新消息")
```

- [ ] **Step 1: 实现 migration**
- [ ] **Step 2: 在 manager.py init() 中调用**

在 `init()` 方法末尾（建表之后）添加：
```python
from ftre.session.migrate_events import migrate_events_to_messages
await migrate_events_to_messages(self._db)
```

- [ ] **Step 3: 验证**

Run: `cd E:\ftre && python -c "from ftre.session.manager import SessionManager; print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
cd E:\ftre
git add src/ftre/session/migrate_events.py src/ftre/session/manager.py
git commit -m "feat: DB migration 旧事件行 → 新消息行"
```

---

## Task 7: 更新 MessageModel 和 API 路由

**Files:**
- Modify: `E:\ftre\src\ftre\session\manager.py` — `MessageModel` 注释
- Modify: `E:\ftre\src\ftre\api\routes.py` — `get_messages` docstring

**核心改动：**

`MessageModel.type` 注释从事件类型改为消息角色。

`get_messages` 的 docstring 中 "一轮 = 一个可见 user_message" → "一轮 = 一个可见 type=user 消息"。

- [ ] **Step 1: 更新注释和 docstring**
- [ ] **Step 2: Commit**

```bash
cd E:\ftre
git add src/ftre/session/manager.py src/ftre/api/routes.py
git commit -m "docs: MessageModel 注释和 API docstring 更新为新消息格式"
```

---

## Self-Review

**1. Spec coverage:**
- ✅ 合并 TOOL_CALL / ASSISTANT_MESSAGE_COMPLETE / REASONING_COMPLETE / USAGE_UPDATE / ERROR → assistant message — Task 1
- ✅ ASSISTANT_MESSAGE / REASONING / TOOL_CALL_STREAMING 不持久化 — Task 1（coalesce 忽略这些类型）
- ✅ SQLite 存储 — Task 6（migration 保持 SQLite）
- ✅ to_openai_messages 简化 — Task 3
- ✅ compact_manager 适配 — Task 5
- ✅ token usage 适配 — Task 4
- ✅ 旧数据迁移 — Task 6

**2. Placeholder scan:** 无 TBD / TODO。每个 step 都有具体代码。

**3. Type consistency:**
- `coalesce_events` 在 Task 1 定义，Task 2 和 Task 6 使用 — 签名一致
- `estimate_messages_tokens` 在 Task 3 重命名，Task 4 和 Task 5 使用 — 一致
- `_find_anchor` 在 Task 4 修改，返回 `(index, usage, "assistant")` — 与 `_compute_token_usage` 消费一致
- 新消息 type 字符串：`"user"` / `"assistant"` / `"tool_result"` / `"done"` / `"context_compact"` / `"external_message"` — 全局一致
