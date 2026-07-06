# Assistant Message Kind 字段实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `AssistantMessageCompleteEvent` 加 `kind` 字段（`"block"` / `"final"`），让 Octo Channel 区分中间块和最终回复，对齐 OpenClaw 的 deliver 回调机制。

**Architecture:** Runner 产出 `assistant_message_complete` 时，根据本轮是否有 tool_calls 设置 `kind`。`"block"` = 有工具调用，后面还要继续；`"final"` = 无工具调用，agent 即将结束。Octo Channel 的 `send()` 缓冲 `block`，立即发送 `final`，`done` 事件时补发未发送的缓冲。

**Tech Stack:** Python, ftre-agent-core, ftre, octo-plugin

## Global Constraints

- `kind` 默认值为 `"final"`，现有消费者不读 `kind` 不受影响
- 不改事件类型名称，只在 data 里加字段
- Octo Channel 是唯一需要感知 `kind` 的消费者
- 前端 ws_channel 自动透传 data，不需要改

---

## 文件结构

| 文件 | 改动 | 职责 |
|------|------|------|
| `E:\ftre-agent-core\src\ftre_agent_core\agent\event.py` | 修改 | 事件定义：`AssistantMessageCompleteEvent` 加 `kind` 字段 |
| `E:\ftre-agent-core\src\ftre_agent_core\agent\runner\react_runner.py` | 修改 | Runner：`_build_complete_events` 传 `kind` |
| `C:\Users\蒋全明\.ftre\plugins\octo_plugin\_channel.py` | 修改 | Octo send()：缓冲 block / 立即发 final / done 补发 |

---

### Task 1: event.py — AssistantMessageCompleteEvent 加 kind 字段

**Files:**
- Modify: `E:\ftre-agent-core\src\ftre_agent_core\agent\event.py`

**Interfaces:**
- Produces: `AssistantMessageCompleteEvent(content: str, kind: str = "final")`
- Produces: `assistant_message_complete_event(content: str, kind: str = "final") -> AgentEvent`
- Produces: `AssistantMessageCompleteData` TypedDict 加 `kind: str`（optional）

- [ ] **Step 1: 修改 AssistantMessageCompleteData TypedDict**

在 `event.py` 第 60-61 行，给 TypedDict 加 `kind`：

```python
class AssistantMessageCompleteData(TypedDict, total=False):
    content: str
    kind: str
```

- [ ] **Step 2: 修改 AssistantMessageCompleteEvent dataclass**

在 `event.py` 第 184-192 行，给 dataclass 加 `kind` 字段：

```python
@dataclass
class AssistantMessageCompleteEvent(AgentEvent):
    content: str
    kind: str = "final"

    def __post_init__(self):
        object.__setattr__(self, 'type', EventType.ASSISTANT_MESSAGE_COMPLETE)

    def _data_dict(self) -> dict:
        return {"content": self.content, "kind": self.kind}
```

- [ ] **Step 3: 修改构造函数**

在 `event.py` 第 455-456 行：

```python
def assistant_message_complete_event(content: str, kind: str = "final") -> AgentEvent:
    return AssistantMessageCompleteEvent(content=content, kind=kind)
```

- [ ] **Step 4: 修改 _from_type 反序列化**

在 `event.py` 第 361-362 行：

```python
    elif t == EventType.ASSISTANT_MESSAGE_COMPLETE:
        return AssistantMessageCompleteEvent(
            content=data.get("content", ""),
            kind=data.get("kind", "final"),
        )
```

- [ ] **Step 5: 语法验证**

Run: `python -c "import ast; ast.parse(open('src/ftre_agent_core/agent/event.py', encoding='utf-8').read()); print('OK')"`
Expected: OK

- [ ] **Step 6: Commit**

```bash
cd E:\ftre-agent-core
git add src/ftre_agent_core/agent/event.py
git commit -m "feat: AssistantMessageCompleteEvent 加 kind 字段（block/final）"
```

---

### Task 2: react_runner.py — _build_complete_events 传 kind

**Files:**
- Modify: `E:\ftre-agent-core\src\ftre_agent_core\agent\runner\react_runner.py:432-452`

**Interfaces:**
- Consumes: `assistant_message_complete_event(content: str, kind: str = "final")` from Task 1
- Produces: `_build_complete_events(persist_memory=False, kind="final")` 产出带 kind 的事件

- [ ] **Step 1: 修改 _build_complete_events 函数签名和内部调用**

第 432 行改为：

```python
        def _build_complete_events(persist_memory: bool = False, kind: str = "final") -> list:
```

第 451 行改为：

```python
            if full_text:
                events.append(assistant_message_complete_event(content=full_text, kind=kind))
```

- [ ] **Step 2: 修改阶段 2 调用点——正常路径**

第 508 行，正常路径产出 complete 事件时传入 kind：

```python
        # ── 阶段 2：输出完整文本/reasoning 事件 ──
        full_text = "".join(text_parts)
        full_reasoning = "".join(reasoning_parts)
        _kind = "block" if tool_calls else "final"
        for ev in _build_complete_events(kind=_kind):
            yield ev
```

- [ ] **Step 3: 确认异常路径不需要改**

第 500 行的 `_build_complete_events(persist_memory=True)` 是取消/异常路径。此时 kind 保持默认 `"final"`（异常中断的文本按 final 处理，让 Octo 补发给用户）。不改。

- [ ] **Step 4: 语法验证**

Run: `python -c "import ast; ast.parse(open('src/ftre_agent_core/agent/runner/react_runner.py', encoding='utf-8').read()); print('OK')"`
Expected: OK

- [ ] **Step 5: 跑现有测试确认无回归**

Run: `cd E:\ftre && python -m pytest tests/test_context_config.py tests/test_agent_manager.py tests/test_plugin_tools.py -q`
Expected: 41 passed

- [ ] **Step 6: Commit**

```bash
cd E:\ftre-agent-core
git add src/ftre_agent_core/agent/runner/react_runner.py
git commit -m "feat: runner 产出 assistant_message_complete 时按 tool_calls 设置 kind（block/final）"
```

---

### Task 3: _channel.py — send() 区分 block/final，缓冲 + done 补发

**Files:**
- Modify: `C:\Users\蒋全明\.ftre\plugins\octo_plugin\_channel.py:554-625`

**Interfaces:**
- Consumes: `event_data["kind"]` 字段，值为 `"block"` 或 `"final"`

- [ ] **Step 1: 在 __init__ 中加缓冲字典**

在 `OctoChannel.__init__` 方法中（`_session_bots` 初始化附近），加：

```python
        self._deliver_buffer: dict[str, str] = {}  # session_id → buffered block text
        self._final_sent: set[str] = set()  # session_ids that already sent final
```

找到 `self._session_bots: dict[str, str] = {}` 那一行，在其后加上这两行。

- [ ] **Step 2: 重写 send() 方法**

将 `send()` 方法（第 554-625 行）替换为：

```python
    async def send(self, msg: Any) -> None:
        """将 AgentLoop 产生的回复发送回 Octo。

        区分 block（中间块，有工具调用，后面继续）和 final（最终回复）：
        - block: 缓冲到 _deliver_buffer，不立即发送
        - final: 立即发送，清空缓冲
        - done: agent 结束，如果有未发送的缓冲则补发
        """
        if not hasattr(msg, 'data') or not isinstance(msg.data, dict):
            return

        event_type: str = msg.data.get("type", "")
        event_data: dict[str, Any] = msg.data.get("data", {})

        session_id: str = msg.to_session or msg.from_session

        if event_type == "assistant_message_complete":
            content: str = event_data.get("content", "")
            if not content:
                return

            kind: str = event_data.get("kind", "final")

            if kind == "block":
                # 中间块：缓冲，不立即发送
                self._deliver_buffer[session_id] = content
                logger.info(f"[octo] 缓冲中间块: session={session_id} 长度={len(content)}")
                return

            # final：立即发送
            self._final_sent.add(session_id)
            self._deliver_buffer.pop(session_id, None)
            await self._send_reply(session_id, content)

        elif event_type == "done":
            # agent 结束，检查是否有未发送的缓冲
            buffered = self._deliver_buffer.pop(session_id, None)
            if buffered and session_id not in self._final_sent:
                logger.info(f"[octo] 补发缓冲: session={session_id} 长度={len(buffered)}")
                await self._send_reply(session_id, buffered)
            self._final_sent.discard(session_id)

    async def _send_reply(self, session_id: str, content: str) -> None:
        """实际发送回复到 Octo 频道（含 @mention 解析和 seq 记录）。"""
        bot_id = self._session_bots.get(session_id, "")
        bot_info = self._bots.get(bot_id)
        if bot_info is None:
            logger.warning(f"[octo] 找不到 session 对应的 bot: session_id={session_id}")
            return

        bot_api: OctoBotApi = bot_info["api"]

        parsed = parse_session_id(session_id)
        if parsed is None and self.session_manager is not None:
            external = await self.session_manager.get_external_session(session_id)
            if external:
                data = external.get("external_data") or {}
                try:
                    parsed = (int(data["channel_type"]), str(data["channel_id"]), str(data.get("bot_id", "")))
                except (KeyError, TypeError, ValueError):
                    parsed = None
        if parsed is None:
            logger.warning(f"[octo] 无法解析 session_id: {session_id}")
            return

        channel_type, channel_id, _ = parsed
        logger.info(f"[octo] 回复目标: channel_type={channel_type} channel_id={channel_id} agent_id={bot_info['agent_id']}")

        try:
            mention_uids: list[str] = []
            def _replace_mention(m: re.Match) -> str:
                uid = m.group(1)
                name = m.group(2)
                if uid not in mention_uids:
                    mention_uids.append(uid)
                return f"@{name}"

            content = re.sub(r"@\[([a-f0-9]{32}):([^\]]+)\]", _replace_mention, content)

            result = await bot_api.send_message(
                channel_id=channel_id,
                channel_type=channel_type,
                content=content,
                mention_uids=mention_uids if mention_uids else None,
            )
            logger.info(f"[octo] 回复发送成功: message_id={result.get('message_id')}")
            inbound_seq = take_pending_inbound_seq(session_id)
            if inbound_seq:
                record_bot_reply(channel_id, inbound_seq, bot_id)
        except Exception:
            logger.exception("[octo] 回复发送失败")
```

- [ ] **Step 3: 恢复 parse_session_id import**

`_send_reply` 用到 `parse_session_id`，需要恢复之前删除的 import：

在 `from _api import (...)` 中加回 `parse_session_id`：

```python
from _api import (
    OctoBotApi,
    CHANNEL_TYPE_DM,
    CHANNEL_TYPE_GROUP,
    CHANNEL_TYPE_THREAD,
    build_external_key,
    build_session_id,
    extract_parent_group_no,
    parse_session_id,
)
```

- [ ] **Step 4: 恢复 re import**

`_send_reply` 用到 `re.sub`，需要恢复 import：

```python
import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any
```

- [ ] **Step 5: 语法验证**

Run: `cd C:\Users\蒋全明\.ftre\plugins\octo_plugin && python -c "import ast; ast.parse(open('_channel.py', encoding='utf-8').read()); print('OK')"`
Expected: OK

- [ ] **Step 6: Commit**

```bash
cd C:\Users\蒋全明\.ftre\plugins\octo_plugin
git add _channel.py
git commit -m "feat: send() 区分 block/final，缓冲中间块，final 立即发送，done 补发"
```

---

## Self-Review

**1. Spec coverage:**
- ✅ `AssistantMessageCompleteEvent` 加 `kind` 字段 — Task 1
- ✅ Runner 根据 `tool_calls` 设置 `kind` — Task 2
- ✅ Octo send() 缓冲 block / 立即发 final / done 补发 — Task 3

**2. Placeholder scan:** 无 TBD / TODO / "add error handling" 等。

**3. Type consistency:**
- Task 1 产出 `assistant_message_complete_event(content: str, kind: str = "final")`
- Task 2 消费 `assistant_message_complete_event(content=full_text, kind=kind)` ✅
- Task 3 消费 `event_data.get("kind", "final")` ✅
- `_build_complete_events(kind=_kind)` 签名匹配 ✅
- `_send_reply(session_id, content)` 在 Task 3 内定义和使用 ✅
