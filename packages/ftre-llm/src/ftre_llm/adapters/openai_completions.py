"""OpenAI Chat Completions 适配器（PRD-B2 FR3）。

迁移自 completion.py 的 _stream_completions 路径，产出改为 StreamChunk。

OpenAI Chat 的增量没有显式块边界，这里用保守策略：
- reasoning（reasoning_content）与 text 各自最多一个块，首个非空 delta 开块
- 工具调用按 wire index 每个一块，首个参数片段开块
- 流末统一闭块（chat 协议没有 per-block 结束事件）
- finish_reason 映射 DSH 词汇：stop / tool-calls / max-tokens / error
  （unknown / None / 其他畸形值 → error finish，接住如 Muse 的 finish_reason: null）
- usage chunk 在 finish 前
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncGenerator
from typing import Any

from ..base import OpenAIAdapterBase, _coerce_request
from ..errors import get_attr
from ..events import (
    BlockEnd,
    BlockStart,
    FinishChunk,
    FinishReason,
    LlmFailure,
    ReasoningDeltaChunk,
    StreamChunk,
    TextDeltaChunk,
    ToolCallDeltaChunk,
    UsageChunk,
)
from ..utils import LLMLogger
from ..wire.normalize import _normalize_chat_messages, normalize_usage

logger = logging.getLogger(__name__)

# wire finish_reason → DSH kind
_FINISH_KIND_MAP = {
    "stop": "stop",
    "tool_calls": "tool-calls",
    "length": "max-tokens",
}


class _ToolCallAccumulator:
    """按 OpenAI streaming 的 index 累积 tool_call delta。

    迁移自 completion.py；返回值改为 (wire_index, call_id, name, fragment) 元组，
    由适配器映射为 chunk 序列。
    """

    def __init__(self):
        # wire index -> {id, name, arguments, emitted_length}
        self._items: dict[int, dict] = {}

    @property
    def has_data(self) -> bool:
        return bool(self._items)

    def feed(self, tc_delta: Any) -> tuple[int, str, str, str] | None:
        """喂入一个 delta；包含新参数片段时返回 (wire_index, id, name, fragment)。"""
        index = get_attr(tc_delta, "index")
        if index is None:
            index = len(self._items)

        entry = self._items.setdefault(
            index,
            {"id": "", "name": "", "arguments": "", "emitted_length": 0},
        )

        function = get_attr(tc_delta, "function")
        call_id = get_attr(tc_delta, "id") or ""
        name = get_attr(function, "name") or ""
        args_fragment = get_attr(function, "arguments") or ""

        if call_id:
            entry["id"] = call_id
        if name:
            entry["name"] = name
        if args_fragment:
            entry["arguments"] += args_fragment

        # Provider 可能先发送 arguments、随后才发送 id/name。参数仍按 wire
        # index 缓存；等 call_id 可用于上层事件时，一次补发尚未发出的前缀，
        # 避免首个 JSON 分片被静默丢弃。
        if entry["id"] and len(entry["arguments"]) > entry["emitted_length"]:
            start = entry["emitted_length"]
            fragment = entry["arguments"][start:]
            entry["emitted_length"] = len(entry["arguments"])
            return (index, entry["id"], entry["name"], fragment)
        return None

    def entries(self) -> list[dict]:
        """流结束后按 wire index 排序返回 {index, id, name, arguments}。"""
        return [
            {
                "index": idx,
                "id": entry["id"],
                "name": entry["name"],
                "arguments": entry["arguments"],
            }
            for idx, entry in sorted(self._items.items())
        ]


class OpenAICompletionsAdapter(OpenAIAdapterBase):
    """chat/completions 协议适配器。"""

    async def stream(
        self,
        messages,
        tools: list[dict] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        messages, tools = _coerce_request(messages, tools)
        llm_log = LLMLogger(self.model)
        response = None
        emitted_finish = False
        try:
            request_messages = _normalize_chat_messages(messages)
            # Responses 的原始 Output Item 只属于 responses 适配器；不能把
            # Host 为下一轮 Responses 重放保留的扩展字段泄漏给 Chat API。
            for message in request_messages:
                message.pop("responses_output_items", None)
                message.pop("response_metadata", None)
            llm_log.log_input(request_messages, tools)

            params: dict[str, Any] = {
                "model": self.model,
                "messages": request_messages,
                "stream": True,
                "stream_options": {"include_usage": True},
                "tool_choice": "auto",
            }
            if self.max_tokens is not None:
                params["max_tokens"] = max(1, int(self.max_tokens))
            if self.temperature is not None:
                params["temperature"] = self.temperature
            if self.reasoning_effort:
                params["reasoning_effort"] = self.reasoning_effort
                # thinking 参数仅 DeepSeek 模型支持
                if "deepseek" in self.model.lower():
                    params["extra_body"] = {"thinking": {"type": "enabled"}}
            if self.stop:
                params["stop"] = list(self.stop)
            if tools:
                params["tools"] = tools

            self._active_loop = asyncio.get_running_loop()
            response = await self._client.chat.completions.create(**params)
            self._active_stream = response

            accumulator = _ToolCallAccumulator()
            usage: dict | None = None
            finish_reason: str = "unknown"
            response_metadata: dict[str, Any] = {}

            # 块分配：reasoning / text 各最多一块（首个非空 delta 开块），
            # 工具调用按 wire index 一块。全文在循环中累积，流末闭块。
            reasoning_parts: list[str] = []
            text_parts: list[str] = []
            reasoning_index: int | None = None
            text_index: int | None = None
            next_index = 0
            tool_block_index: dict[int, int] = {}  # wire index → block index

            async for chunk in response:
                if self._cancelled:
                    logger.info("[completion] stream cancelled by cancel()")
                    break

                llm_log.log_chunk(chunk)

                for key in ("id", "model", "created", "system_fingerprint"):
                    value = get_attr(chunk, key)
                    if value is not None:
                        response_metadata[key] = value

                # OpenAI 会在最后额外返回一个 usage-only chunk。
                chunk_usage = normalize_usage(get_attr(chunk, "usage"))
                if chunk_usage:
                    usage = chunk_usage

                choices = get_attr(chunk, "choices", []) or []
                if not choices:
                    continue

                choice = choices[0]
                delta = get_attr(choice, "delta")

                # 部分推理模型会把 reasoning 放在 reasoning_content 字段。
                reasoning = get_attr(delta, "reasoning_content")
                if reasoning:
                    if reasoning_index is None:
                        reasoning_index = next_index
                        next_index += 1
                        yield BlockStart(index=reasoning_index, block_type="reasoning")
                    reasoning_parts.append(reasoning)
                    yield ReasoningDeltaChunk(index=reasoning_index, text=reasoning)

                # 普通 assistant 文本。
                content = get_attr(delta, "content")
                if content:
                    if text_index is None:
                        text_index = next_index
                        next_index += 1
                        yield BlockStart(index=text_index, block_type="text")
                    text_parts.append(content)
                    yield TextDeltaChunk(index=text_index, text=content)

                # 工具调用参数的 JSON 流式片段。
                tc_deltas = get_attr(delta, "tool_calls") or []
                for tc_delta in tc_deltas:
                    event = accumulator.feed(tc_delta)
                    if event is not None:
                        wire_index, call_id, name, fragment = event
                        if wire_index not in tool_block_index:
                            tool_block_index[wire_index] = next_index
                            next_index += 1
                            yield BlockStart(index=tool_block_index[wire_index], block_type="tool-call")
                        yield ToolCallDeltaChunk(
                            index=tool_block_index[wire_index],
                            call_id=call_id,
                            name=name,
                            arguments_delta=fragment,
                        )

                fr = get_attr(choice, "finish_reason")
                if fr:
                    finish_reason = fr

            # ── 流末收尾：闭块 → usage → finish ─────────────────────────
            if reasoning_index is not None:
                yield BlockEnd(
                    index=reasoning_index,
                    block={"type": "thinking", "thinking": "".join(reasoning_parts)},
                )
            if text_index is not None:
                yield BlockEnd(
                    index=text_index,
                    block={"type": "text", "text": "".join(text_parts)},
                )
            for entry in accumulator.entries():
                if not entry["id"] or not entry["name"]:
                    continue
                yield BlockEnd(
                    index=tool_block_index[entry["index"]],
                    block={
                        "type": "tool-call",
                        "id": entry["id"],
                        "name": entry["name"],
                        "arguments": entry["arguments"],
                    },
                )

            if usage is not None:
                yield UsageChunk(usage=usage)

            # finish_reason 语义映射：未知 / None（畸形，如 Muse）→ 有内容产出时
            # 宽容映射 stop（内容完整可信，不浪费一次成功调用），无产出时 error
            kind = _FINISH_KIND_MAP.get(finish_reason)
            if kind is None:
                has_output = bool(reasoning_parts or text_parts or accumulator.has_data)
                if has_output:
                    logger.warning(
                        "[completion] stream ended with unknown finish_reason=%s but has output; mapping to stop",
                        finish_reason,
                    )
                    kind = "stop"
                else:
                    logger.warning(
                        "[completion] stream ended: finish_reason=%s has_tool_calls=%s",
                        finish_reason,
                        accumulator.has_data,
                    )
                    failure = LlmFailure(
                        message=f"model stopped: {finish_reason}",
                        code=str(finish_reason or "unknown").upper(),
                    )
                    yield FinishChunk(reason=FinishReason(
                        kind="error", failure=failure, raw=finish_reason or "",
                        response_metadata=response_metadata,
                    ))
                    emitted_finish = True
                    # 跳过正常 finish 产出
                    kind = None
            if kind is not None:
                yield FinishChunk(reason=FinishReason(
                    kind=kind, raw=finish_reason,
                    response_metadata=response_metadata,
                ))
                emitted_finish = True

        except Exception as exc:  # noqa: BLE001 - normalize provider failures
            from ..errors import LLMError
            err = LLMError.classify(exc)
            logger.warning("[adapter] completions stream failed: %s (%s)", err.message[:200], err.code)
            if not emitted_finish:
                yield FinishChunk(reason=FinishReason(
                    kind="error",
                    failure=LlmFailure(message=err.message, code=err.code),
                ))
                emitted_finish = True
        finally:
            self._active_stream = None
            self._active_loop = None
            was_cancelled = self._cancelled
            self._cancelled = False
            if response is not None:
                close_result = response.close()
                if inspect.isawaitable(close_result):
                    try:
                        await close_result
                    except Exception:
                        logger.debug("failed to close completions response", exc_info=True)
            llm_log.flush()
        # 取消路径：子循环 break 后未发 finish（正常路径已在上面发出）
        if not emitted_finish:
            if was_cancelled:
                yield FinishChunk(reason=FinishReason(
                    kind="aborted",
                    failure=LlmFailure(message="stream cancelled by cancel()", code="ABORTED"),
                ))
            else:
                yield FinishChunk(reason=FinishReason(
                    kind="error",
                    failure=LlmFailure(message="stream ended without finish chunk", code="STREAM_CLOSED"),
                ))

