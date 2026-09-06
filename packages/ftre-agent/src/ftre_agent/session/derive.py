"""derive_messages——事件日志 → Msg 列表的幂等纯函数 fold（PRD-F43 FR8/FR9）。

读侧唯一投影：HTTP /messages、LLM 上下文构建、compact 锚点裁剪都从这里出。
客户端 ConversationAssembler（F42）实现同一规则，golden fixture 对拍。

fold 规则（与 F41 §4.2 surface 列一致）：
  user/message      → 新 UserMsg（并封口上一条未完成 assistant——steering 边界）
  assistant/message → whole-value 替换该 message_id 的聚合态
  assistant/chunk   → 把 text/thinking/tool_result_text 折叠进进行中的消息
  tool/call-start   → 确保 assistant 消息存在（流式期占位）+ 追加 ToolCallBlock
  tool/result       → 追加 ToolResultBlock + 配对 ToolCall 置 finished
  hint/message      → 追加 HintBlock
  compact/message   → compact 锚点 UserMsg（name=compact/compact_fast）
  turn/end          → 对 message_id 消息落终态（finished_at/reason/error/token）
  tool/result-start / approval / turn.start·retry / session/status → 不产消息
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ftre_agent.message import (
    DataBlock,
    HintBlock,
    Msg,
    MsgName,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
)
from ftre_agent.message import (
    UserMsg as _user_msg_factory,
)
from ftre_agent.message._msg import MsgToken, TokenUsage


def _iso_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def _find_block(message: Msg, block_type: str, block_id: str):
    for block in message.content:
        if block.type == block_type and block.id == block_id:
            return block
    return None


def _chunk_block_id(data: dict[str, Any], *, kind: str, message_id: str) -> str:
    """返回 chunk 的稳定块 id；旧事件缺 id 时按消息和块类型归一。"""
    block_id = data.get("block_id")
    if isinstance(block_id, str) and block_id:
        return block_id
    return f"assistant_{kind}_{message_id}"


def _normalize_user_content(
    content: Any,
    *,
    message_id: str,
    time_iso: str,
) -> list[Any]:
    """把开放的 user/message parts 归一为可验证的 Msg blocks。

    ``user/message`` 是 Host 与 UI 的扩展边界，可能带 ``skill``、
    ``image_file`` 等非 LLM 类型；Msg 本身只接受 text/data。读取侧不能因为
    一个未知 UI part 让整个会话历史失效，因此保留可读语义并降级为 TextBlock。
    block id 和缺失时间由事件坐标派生，保证多次 derive 结果完全一致。
    """
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []

    blocks: list[Any] = []
    for index, raw in enumerate(content):
        part = dict(raw) if isinstance(raw, dict) else {"type": "text", "text": str(raw)}
        part_type = str(part.get("type") or "text")
        block_id = str(part.get("id") or f"{message_id}:block:{index}")
        created_at = str(part.get("created_at") or time_iso)
        finished_at = part.get("finished_at")

        if part_type == "text":
            blocks.append(
                TextBlock(
                    text=str(part.get("text") or part.get("data") or ""),
                    id=block_id,
                    created_at=created_at,
                    finished_at=finished_at,
                )
            )
            continue

        if part_type == "data":
            data_block = None
            try:
                data_block = DataBlock.model_validate(part).model_copy(
                    update={
                        "id": block_id,
                        "created_at": created_at,
                        "finished_at": finished_at,
                    }
                )
            except (TypeError, ValueError):
                # 旧 UI 可能使用 {type:image, data:{...}}；下面统一降级，
                # 不能因为图片字段形状变化阻断文本历史恢复。
                data_block = None
            if data_block is not None:
                blocks.append(data_block)
                continue

        # UI 扩展 part（skill/image/image_file/image_url/未来类型）没有
        # 对应的 LLM Msg Block。保留最有用的可读值，而不是静默丢弃。
        value = part.get("text")
        if value is None:
            value = part.get("data")
        if value is None:
            value = part.get("path") or part.get("url")
        if isinstance(value, (dict, list)):
            import json

            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        label = f"[{part_type}]" if part_type != "skill" else "[skill]"
        text = f"{label} {value}".strip() if value not in (None, "") else label
        blocks.append(
            TextBlock(
                text=text,
                id=block_id,
                created_at=created_at,
                finished_at=finished_at,
            )
        )
    return blocks


class _FoldState:
    """fold 过程态：消息表（保持插入序）+ tool_call 归属索引。"""

    def __init__(self) -> None:
        self.messages: dict[str, Msg] = {}
        self.order: list[str] = []
        self.tool_calls: dict[str, str] = {}

    def ensure_assistant(self, message_id: str, created_ms: int) -> Msg:
        message = self.messages.get(message_id)
        if message is None:
            message = Msg(
                id=message_id,
                name=MsgName.DEFAULT,
                content=[],
                role="assistant",
                created_at=_iso_from_ms(created_ms),
            )
            self.insert(message)
        return message

    def insert(self, message: Msg) -> None:
        if message.id not in self.messages:
            self.order.append(message.id)
        self.messages[message.id] = message
        for block in message.content:
            if block.type == "tool_call":
                self.tool_calls[block.id] = message.id

    def seal_previous_assistant(self, finished_at: str) -> None:
        """真实用户消息到达时封口上一条未完成 assistant（steering/新轮边界）。"""
        for message_id in reversed(self.order):
            message = self.messages[message_id]
            if message.role == "assistant":
                if message.finished_at is None:
                    message.finished_at = finished_at
                    message.finished_reason = "completed"
                return
            if message.role == "user":
                return


def _tool_owner(state: _FoldState, tool_call_id: str) -> str | None:
    owner_id = state.tool_calls.get(tool_call_id)
    if owner_id is not None:
        return owner_id
    for candidate_id in reversed(state.order):
        candidate = state.messages[candidate_id]
        if candidate.role == "assistant" and _find_block(
            candidate, "tool_call", tool_call_id
        ) is not None:
            return candidate_id
    return None


def _append_assistant_chunk(
    state: _FoldState, event: dict[str, Any], data: dict[str, Any]
) -> None:
    message_id = str(event.get("message_id") or "")
    kind = str(data.get("kind") or "")
    if not message_id or kind not in {"text", "thinking"}:
        return
    time_ms = int(event.get("time") or 0)
    message = state.ensure_assistant(message_id, time_ms)
    block_id = _chunk_block_id(data, kind=kind, message_id=message_id)
    delta = str(data.get("delta") or "")
    block = _find_block(message, kind, block_id)
    if kind == "text":
        if isinstance(block, TextBlock):
            block.text += delta
        else:
            message.content.append(
                TextBlock(
                    text=delta,
                    id=block_id,
                    created_at=_iso_from_ms(time_ms),
                )
            )
    elif isinstance(block, ThinkingBlock):
        block.thinking += delta
    else:
        message.content.append(
            ThinkingBlock(
                thinking=delta,
                id=block_id,
                created_at=_iso_from_ms(time_ms),
            )
        )


def _append_tool_result_chunk(
    state: _FoldState, event: dict[str, Any], data: dict[str, Any]
) -> None:
    tool_call_id = str(data.get("tool_call_id") or "")
    if not tool_call_id:
        return
    owner_id = _tool_owner(state, tool_call_id)
    if owner_id is None:
        return
    time_ms = int(event.get("time") or 0)
    message = state.messages[owner_id]
    existing = _find_block(message, "tool_result", tool_call_id)
    if isinstance(existing, ToolResultBlock) and existing.finished_at is not None:
        return
    if existing is None:
        call = _find_block(message, "tool_call", tool_call_id)
        existing = ToolResultBlock(
            id=tool_call_id,
            name=str(getattr(call, "name", "") or ""),
            output=[],
            state=ToolResultState.RUNNING,
            metadata={},
            created_at=_iso_from_ms(time_ms),
        )
        message.content.append(existing)
    if isinstance(existing.output, str):
        existing.output = [
            TextBlock(
                text=existing.output,
                id=f"tool_result_{tool_call_id}_text",
                created_at=existing.created_at,
            )
        ]
    elif not isinstance(existing.output, list):
        existing.output = []
    delta = str(data.get("delta") or "")
    last = existing.output[-1] if existing.output else None
    if isinstance(last, TextBlock):
        last.text += delta
    else:
        existing.output.append(
            TextBlock(
                text=delta,
                id=f"tool_result_{tool_call_id}_text",
                created_at=_iso_from_ms(time_ms),
            )
        )


def derive_messages(events: list[dict[str, Any]]) -> list[Msg]:
    """把事件序列 fold 为有序 Msg 列表（全量 transcript，含 hide 消息）。"""
    state = _FoldState()
    for event in events:
        _apply_event(state, event)
    return [state.messages[message_id] for message_id in state.order]


def derive_context_messages(events: list[dict[str, Any]]) -> list[Msg]:
    """LLM 上下文视图：最后一条 summary compact 为锚点（含锚点），
    fast 模式 compact 累计裁剪的 tool_result 输出置为占位文本。"""
    messages = derive_messages(events)
    anchor_index = -1
    for index, message in enumerate(messages):
        if message.role == "user" and message.name == MsgName.COMPACT:
            anchor_index = index

    if anchor_index >= 0:
        compact = messages[anchor_index]
        compact_meta = compact.metadata.get("context_compact") or {}
        through_id = str(compact_meta.get("through_message_id") or "")
        if through_id:
            through_index = next(
                (index for index, item in enumerate(messages) if item.id == through_id),
                -1,
            )
        else:
            through_index = -1
        if through_index >= 0:
            # compact 事件通常位于 through 消息之后；把摘要提升到首位，
            # 只保留 through 之后的消息（包括压缩期间到达的消息）。
            tail = [compact, *(
                item for item in messages[through_index + 1:]
                if item.id != compact.id
            )]
        else:
            tail = [compact, *messages[anchor_index + 1:]]
    else:
        tail = messages

    # fast compact 新事件持久化精确的 tool_result_ids；旧日志没有该字段时，
    # 仅从该 compact 事件之前选择 n 个结果，避免旧 compact 误裁未来输出。
    tool_result_event_order = [
        (int(event.get("seq") or 0), str((event.get("data") or {}).get("tool_call_id") or ""))
        for event in events
        if event.get("type") == "tool/result"
        and (event.get("data") or {}).get("tool_call_id")
    ]
    trim_ids: set[str] = set()
    for event in events:
        if event.get("type") != "compact/message":
            continue
        data = event.get("data") or {}
        if data.get("mode") != "fast":
            continue
        ids = data.get("tool_result_ids") or []
        if isinstance(ids, list) and ids:
            trim_ids.update(str(item) for item in ids if item)
            continue
        count = max(0, int(data.get("tool_results") or 0))
        cutoff = int(event.get("seq") or 0)
        if count:
            eligible = [
                tool_id
                for seq, tool_id in tool_result_event_order
                if seq < cutoff and tool_id not in trim_ids
            ]
            trim_ids.update(eligible[:count])

    if trim_ids:
        for message in tail:
            for block in message.content:
                if block.type == "tool_result" and block.id in trim_ids:
                    block.output = [TextBlock(text="[已压缩裁剪：原始工具输出不再可见]")]
                    block.metadata = dict(block.metadata or {})
    return tail


def _to_output_blocks(parts: list[Any]) -> list[Any]:
    """tool/result 的 output parts → 类型化 Block（TextBlock/DataBlock）。"""
    blocks: list[Any] = []
    for part in parts:
        if isinstance(part, dict):
            part_type = part.get("type", "text")
            if part_type == "text":
                from ftre_agent.message import TextBlock as _TextBlock

                blocks.append(_TextBlock.model_validate(part))
            elif part_type == "data":
                from ftre_agent.message import DataBlock as _DataBlock

                blocks.append(_DataBlock.model_validate(part))
            else:
                blocks.append(part)
        else:
            blocks.append(part)
    return blocks


def _apply_event(state: _FoldState, event: dict[str, Any]) -> None:
    type_ = event.get("type")
    data = event.get("data") or {}
    time_ms = int(event.get("time") or 0)
    message_id = event.get("message_id")

    if type_ == "user/message":
        state.seal_previous_assistant(_iso_from_ms(time_ms))
        resolved_message_id = str(message_id or f"user_{event.get('seq', 0)}")
        message = _user_msg_factory(
            content=_normalize_user_content(
                data.get("content") or [],
                message_id=resolved_message_id,
                time_iso=_iso_from_ms(time_ms),
            ),
            id=resolved_message_id,
            created_at=_iso_from_ms(time_ms),
            metadata=dict(data.get("metadata") or {}),
        )
        state.insert(message)
        return

    if type_ == "assistant/message":
        payload = data.get("message") or {}
        message = Msg.model_validate(payload)
        if message_id and message.id != message_id:
            # whole-value 载荷与信封坐标不一致时以信封为准（防御性对齐）
            message = message.model_copy(update={"id": message_id})
        state.insert(message)
        return

    if type_ == "hint/message":
        if not message_id:
            return
        message = state.ensure_assistant(message_id, time_ms)
        # 块 id 派生自事件 seq：两侧 fold（derive / ConversationAssembler）对拍
        # 需要确定性标识，禁止随机生成（PRD-F42 AC1 / F43 AC5）。
        message.content.append(
            HintBlock(
                hint=data.get("hint"),
                source=data.get("source"),
                id=f"hint_{event.get('seq')}",
                created_at=_iso_from_ms(time_ms),
                finished_at=_iso_from_ms(time_ms),
            )
        )
        return

    if type_ == "compact/message":
        state.seal_previous_assistant(_iso_from_ms(time_ms))
        mode = str(data.get("mode") or "summary")
        # 摘要块的 id 派生自 message_id（确定性，客户端 fold 同规则）
        compact_block_id = f"compact_{message_id or ''}"
        if mode == "fast":
            from ftre_agent.message import AssistantMsg as _assistant_factory

            tool_results = int(data.get("tool_results") or 0)
            tokens_before = int(data.get("tokens_before") or 0)
            tokens_after = int(data.get("tokens_after") or 0)
            saved = max(0, tokens_before - tokens_after)
            text = (
                f"已快速压缩：{tool_results} 个较早的工具输出已被裁剪，"
                f"其原始内容不再可见（约节省 {saved} tokens）。"
                "后续如需相关信息请重新获取。"
            )
            message = _assistant_factory(
                name=MsgName.COMPACT_FAST,
                content=[
                    TextBlock(
                        text=text,
                        id=compact_block_id,
                        created_at=_iso_from_ms(time_ms),
                    )
                ],
                id=message_id or "",
                created_at=_iso_from_ms(time_ms),
                finished_at=_iso_from_ms(time_ms),
                finished_reason="completed",
                metadata={
                    "context_compact": {
                        "mode": "fast",
                        "tool_results": tool_results,
                        "tokens_before": tokens_before,
                        "tokens_after": tokens_after,
                    }
                },
            )
        else:
            message = _user_msg_factory(
                name=MsgName.COMPACT,
                content=[
                    TextBlock(
                        text=str(data.get("summary_text") or ""),
                        id=compact_block_id,
                        created_at=_iso_from_ms(time_ms),
                    )
                ],
                id=message_id or "",
                created_at=_iso_from_ms(time_ms),
                metadata={
                    "hide": True,
                    "context_compact": {
                        "through_message_id": data.get("through_message_id", ""),
                        "trigger": data.get("trigger", "auto"),
                        "tokens_before": data.get("tokens_before", 0),
                        "tokens_after": data.get("tokens_after", 0),
                    },
                },
            )
        state.insert(message)
        return

    if type_ == "tool/call-start":
        if not message_id:
            return
        message = state.ensure_assistant(message_id, time_ms)
        tool_call_id = str(data.get("tool_call_id") or "")
        if _find_block(message, "tool_call", tool_call_id) is None:
            message.content.append(
                ToolCallBlock(
                    id=tool_call_id,
                    name=str(data.get("name") or ""),
                    arguments=dict(data.get("arguments") or {}),
                    created_at=_iso_from_ms(time_ms),
                )
            )
            state.tool_calls[tool_call_id] = message.id
        return

    if type_ == "tool/result":
        tool_call_id = str(data.get("tool_call_id") or "")
        owner_id = _tool_owner(state, tool_call_id)
        if owner_id is None:
            return
        message = state.messages[owner_id]
        try:
            block_state = ToolResultState(str(data.get("state") or "success"))
        except ValueError:
            block_state = ToolResultState.SUCCESS
        existing = _find_block(message, "tool_result", tool_call_id)
        if existing is not None:
            message.content.remove(existing)
        message.content.append(
            ToolResultBlock(
                id=tool_call_id,
                name=str(data.get("name") or ""),
                output=_to_output_blocks(list(data.get("output") or [])),
                state=block_state,
                metadata=dict(data.get("metadata") or {}),
                created_at=_iso_from_ms(time_ms),
                finished_at=_iso_from_ms(time_ms),
            )
        )
        call_block = _find_block(message, "tool_call", tool_call_id)
        if call_block is not None:
            call_block.state = ToolCallState.FINISHED
            call_block.finished_at = _iso_from_ms(time_ms)
        return

    if type_ == "assistant/chunk":
        kind = str(data.get("kind") or "")
        if kind in {"text", "thinking"}:
            _append_assistant_chunk(state, event, data)
        elif kind == "tool_result_text":
            _append_tool_result_chunk(state, event, data)
        return

    if type_ == "turn/end":
        if not message_id:
            return
        message = state.messages.get(message_id)
        if message is None or message.role != "assistant":
            return
        if message.finished_at is None:
            message.finished_at = _iso_from_ms(time_ms)
        message.finished_reason = str(data.get("reason") or data.get("outcome") or "completed")
        error = data.get("error")
        if isinstance(error, dict) and error:
            message.error = error
        usage = data.get("usage")
        if isinstance(usage, dict) and usage.get("total_tokens") is not None:
            token = TokenUsage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
            )
            if message.token is None:
                # 没有 assistant/message Token 时仅能把 turn 累计值作为兼容兜底；
                # 正常 Runtime 会在最终 Assistant 快照中先写入 last_call_usage。
                message.token = MsgToken(usage=token, last_call_usage=token)
            else:
                # turn/end 的 usage 是整轮累计值，不能覆盖最近一次调用锚点。
                message.token = message.token.model_copy(update={"usage": token})
        return

    # tool/result-start / approval/asked / turn/start / turn/retry /
    # session/status：不改变消息表面
    return


__all__ = ["derive_context_messages", "derive_messages"]
