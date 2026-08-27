"""OpenAI Responses API 适配器（PRD-B2 FR4）。

迁移自 completion.py 的 _stream_responses 路径，产出改为 StreamChunk。

Responses API 的流事件有显式的 item 边界（OutputItemAdded / OutputItemDone），
比 chat 协议更贴合 StreamChunk：每个 output item 一个块。
- ResponseTextDeltaEvent          → text delta
- ResponseReasoningDeltaEvent     → reasoning delta（网关支持时）
- ResponseFunctionCallArgumentsDeltaEvent → tool-call arguments delta
- ResponseOutputItemAddedEvent    → block-start（function_call；text/reasoning
                                    item 的 start 在首个 delta 时延迟发）
- ResponseOutputItemDoneEvent     → block-end（携带完整 item）
- ResponseCompletedEvent          → usage + finish
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from ..base import OpenAIAdapterBase, _coerce_request
from ..errors import LLMError, get_attr
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


class OpenAIResponsesAdapter(OpenAIAdapterBase):
    """OpenAI Responses API 协议适配器。"""

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
            # Responses API 也从同一份 Chat 消息历史转换而来，先复用同一个工具调用
            # 协议边界，避免把孤立 function_call_output 传给 provider。
            reasoning_enabled = bool(
                self.reasoning_effort and self.reasoning_effort != "none"
            )
            request_messages = _normalize_chat_messages(
                messages,
                preserve_reasoning_only=reasoning_enabled,
            )
            llm_log.log_input(request_messages, tools)

            instructions, input_items = _convert_messages_to_responses_input(
                request_messages,
                include_reasoning=reasoning_enabled,
                allow_legacy_reasoning_content=_legacy_reasoning_content_supported(
                    self.model
                ),
                model=self.model,
            )
            resp_tools = _convert_tools_to_responses(tools) if tools else []

            params: dict[str, Any] = {
                "model": self.model,
                "input": input_items,
                "stream": True,
                "store": False,
                "tool_choice": "auto",
            }
            if instructions is not None:
                params["instructions"] = instructions
            if self.max_tokens is not None:
                params["max_output_tokens"] = max(1, int(self.max_tokens))
            if self.temperature is not None:
                params["temperature"] = self.temperature
            if reasoning_enabled:
                params["reasoning"] = {"effort": self.reasoning_effort}
            if resp_tools:
                params["tools"] = resp_tools

            self._active_loop = asyncio.get_running_loop()
            response = await self._client.responses.create(**params)
            self._active_stream = response

            usage: dict | None = None
            finish_kind: str = "unknown"
            response_metadata: dict[str, Any] = {}
            # item_id → 块信息 {kind, index, started}
            items: dict[str, dict] = {}
            next_index = 0

            async for event in response:
                if self._cancelled:
                    logger.info("[completion] responses stream cancelled by cancel()")
                    break

                llm_log.log_chunk(event)

                event_type = type(event).__name__

                # ── 文本增量 ──
                if event_type == "ResponseTextDeltaEvent":
                    item_id = get_attr(event, "item_id", "")
                    delta = get_attr(event, "delta")
                    if delta:
                        entry = items.setdefault(item_id, {"kind": "text", "index": None, "started": False, "text": ""})
                        if entry["index"] is None:
                            entry["index"] = next_index
                            next_index += 1
                        if not entry["started"]:
                            entry["started"] = True
                            yield BlockStart(index=entry["index"], block_type="text")
                        entry["text"] += delta
                        yield TextDeltaChunk(index=entry["index"], text=delta)

                # ── 推理增量（如果网关支持流式返回推理文本）──
                # OpenAI SDK 的 Responses 事件实际分为 reasoning_text 和
                # reasoning_summary 两种命名；部分旧 fake/网关仍使用
                # ResponseReasoningDeltaEvent。三者都归一为同一个 reasoning 块，
                # 否则真实的 ResponseReasoningTextDeltaEvent 会被静默忽略。
                elif event_type in {
                    "ResponseReasoningDeltaEvent",
                    "ResponseReasoningTextDeltaEvent",
                    "ResponseReasoningSummaryTextDeltaEvent",
                }:
                    item_id = get_attr(event, "item_id", "")
                    delta = get_attr(event, "delta")
                    if delta:
                        entry = items.setdefault(item_id, {"kind": "reasoning", "index": None, "started": False, "text": ""})
                        if entry["index"] is None:
                            entry["index"] = next_index
                            next_index += 1
                        if not entry["started"]:
                            entry["started"] = True
                            yield BlockStart(index=entry["index"], block_type="reasoning")
                        entry["text"] += delta
                        yield ReasoningDeltaChunk(index=entry["index"], text=delta)

                # ── 工具调用参数增量 ──
                elif event_type == "ResponseFunctionCallArgumentsDeltaEvent":
                    item_id = get_attr(event, "item_id", "")
                    delta = get_attr(event, "delta", "")
                    if delta:
                        entry = items.setdefault(item_id, {"kind": "tool-call", "index": None, "started": False})
                        if entry["index"] is None:
                            entry["index"] = next_index
                            next_index += 1
                        if not entry["started"]:
                            entry["started"] = True
                            yield BlockStart(index=entry["index"], block_type="tool-call")
                        # call_id / name 在 OutputItemAdded 时记录
                        yield ToolCallDeltaChunk(
                            index=entry["index"],
                            call_id=entry.get("call_id", ""),
                            name=entry.get("name", ""),
                            arguments_delta=delta,
                        )

                # ── 输出项添加：记录 function_call 的 call_id 和 name ──
                elif event_type == "ResponseOutputItemAddedEvent":
                    item = get_attr(event, "item", {})
                    item_id = get_attr(item, "id", "")
                    if get_attr(item, "type") == "function_call":
                        entry = items.setdefault(item_id, {"kind": "tool-call", "index": None, "started": False})
                        entry["call_id"] = get_attr(item, "call_id", "")
                        entry["name"] = get_attr(item, "name", "")

                # ── 输出项完成：block-end（携带完整 item 内容）──
                elif event_type == "ResponseOutputItemDoneEvent":
                    item = get_attr(event, "item", {})
                    item_id = get_attr(item, "id", "")
                    item_type = get_attr(item, "type", "")
                    # OutputItemDone 是 Responses 多轮重放的唯一完整快照。
                    # 这里保存 JSON-safe 原始字段，不能只依赖 reasoning delta；
                    # Host 会把它写入 Msg metadata，下一轮再由转换器筛选重放。
                    raw_item = _serialize_response_item(item)
                    if raw_item:
                        response_metadata.setdefault("output_items", []).append(raw_item)
                    if item_type == "function_call":
                        entry = items.setdefault(item_id, {"kind": "tool-call", "index": None, "started": False})
                        if entry["index"] is None:
                            entry["index"] = next_index
                            next_index += 1
                        if not entry["started"]:
                            entry["started"] = True
                            yield BlockStart(index=entry["index"], block_type="tool-call")
                        yield BlockEnd(
                            index=entry["index"],
                            block={
                                "type": "tool-call",
                                "id": get_attr(item, "call_id", ""),
                                "name": get_attr(item, "name", ""),
                                "arguments": get_attr(item, "arguments", ""),
                            },
                        )
                        entry["ended"] = True
                        if entry.get("call_id") or get_attr(item, "call_id"):
                            finish_kind = "tool-calls" if finish_kind == "unknown" else finish_kind

                # ── 响应完成 ──
                elif event_type == "ResponseCompletedEvent":
                    resp = get_attr(event, "response", {})
                    usage = normalize_usage(get_attr(resp, "usage"))
                    for key in ("id", "model", "created_at"):
                        value = get_attr(resp, key)
                        if value is not None:
                            response_metadata[key] = value
                    status = get_attr(resp, "status", "")
                    incomplete = get_attr(resp, "incomplete_details", None)
                    if incomplete:
                        finish_kind = "max-tokens"
                    elif finish_kind == "tool-calls":
                        pass
                    elif status == "completed":
                        finish_kind = "stop"

            # ── 流末收尾：闭块 → usage → finish ─────────────────────────
            # text / reasoning item 若未收到 OutputItemDone（部分网关不发），
            # 在流末补 block-end（全文从增量累积重建）。
            for item_id, entry in items.items():
                if entry["kind"] in ("text", "reasoning") and entry.get("started") and not entry.get("ended"):
                    if entry["kind"] == "text":
                        block = {"type": "text", "text": entry.get("text", "")}
                    else:
                        block = {"type": "thinking", "thinking": entry.get("text", "")}
                    yield BlockEnd(index=entry["index"], block=block)

            if usage is not None:
                yield UsageChunk(usage=usage)

            if finish_kind not in ("stop", "tool-calls", "max-tokens"):
                logger.warning(
                    "[completion] responses stream ended: finish_kind=%s items=%s",
                    finish_kind,
                    {k: v["kind"] for k, v in items.items()},
                )
                yield FinishChunk(reason=FinishReason(
                    kind="error",
                    failure=LlmFailure(
                        message=f"responses stream ended with status {finish_kind!r}",
                        code="UNKNOWN_FINISH",
                    ),
                    raw=finish_kind,
                    response_metadata=response_metadata,
                ))
            else:
                yield FinishChunk(reason=FinishReason(
                    kind=finish_kind,
                    raw=finish_kind,
                    response_metadata=response_metadata,
                ))
            emitted_finish = True

        except Exception as exc:  # noqa: BLE001 - normalize provider failures
            err = LLMError.classify(exc)
            logger.warning("[adapter] responses stream failed: %s (%s)", err.message[:200], err.code)
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
                        logger.debug("failed to close responses response", exc_info=True)
            llm_log.flush()
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


def _json_safe(value: Any) -> Any:
    """把 SDK Output Item 转成可持久化的 JSON 值。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(mode="json", exclude_none=True))
        except TypeError:
            return _json_safe(model_dump(exclude_none=True))
    if hasattr(value, "__dict__"):
        return _json_safe(vars(value))
    return str(value)


def _serialize_response_item(item: Any) -> dict[str, Any]:
    """提取 Responses Output Item 的完整 JSON-safe 快照。"""
    value = _json_safe(item)
    return value if isinstance(value, dict) and value.get("type") else {}


def _legacy_reasoning_content_supported(model: str) -> bool:
    """判断是否启用旧式 reasoning_text 回传兼容。

    OpenCode 的 DeepSeek thinking 协议仍要求把 ``reasoning_text`` 放回下一轮；
    GPT Responses 则要求 reasoning input 不带 ``content``。未知模型默认走更
    严格的 input-safe 路径。
    """
    return "deepseek" in model.lower()


def _sanitize_reasoning_item(
    item: Any,
    *,
    allow_content: bool = False,
) -> dict[str, Any] | None:
    """把返回态 reasoning item 转成可重放的 Responses input item。

    ``content`` 是模型返回的 reasoning 文本，不是跨 provider 稳定的 input
    字段。Console Go 的 GPT Responses 对 reasoning input 明确要求 ``content``
    为空；无状态多轮应优先使用供应商返回的 ``encrypted_content``，其次使用
    reasoning ``summary``。仅对明确要求旧式 reasoning_text 的 DeepSeek 兼容路径
    保留 content。完整返回对象仍由 ``_serialize_response_item`` 保存到 response
    metadata，不能把持久化快照和请求协议混为一谈。
    """
    value = _serialize_response_item(item)
    if value.get("type") != "reasoning":
        return None
    allowed = ("type", "id", "summary", "encrypted_content")
    if allow_content:
        allowed += ("content",)
    result = {key: value[key] for key in allowed if key in value}
    # 只有 id/type 而没有 encrypted_content 或 summary 的 reasoning item
    # 无法恢复模型状态；DeepSeek 兼容路径则允许使用返回的 content 恢复。
    if (
        not result.get("encrypted_content")
        and not result.get("summary")
        and not (allow_content and result.get("content"))
    ):
        return None
    return result


def _has_reasoning_text_content(item: dict[str, Any]) -> bool:
    """判断 reasoning item 是否携带可回传的 ``reasoning_text`` 内容块。"""
    blocks = item.get("content")
    if not isinstance(blocks, list):
        return False
    return any(
        isinstance(block, dict)
        and block.get("type") == "reasoning_text"
        and block.get("text")
        for block in blocks
    )


def _replay_items_match_target(
    items: list[dict[str, Any]],
    *,
    allow_content: bool,
    model: str | None = None,
) -> bool:
    """校验历史 reasoning replay 组与当前目标协议是否形状兼容。

    历史快照可能来自另一个 Provider/中转（例如 DeepSeek 中转返回的普通 UUID
    id + ``reasoning_text`` content，或 GPT 返回的 summary-only item）。跨目标
    重放会直接触发上游 400：

      - input-safe 目标（GPT Responses）：要求 id 为 ``rs`` 前缀，且以
        ``encrypted_content`` 或 ``summary`` 恢复状态；带普通 UUID 的 item
        视为外来产物。
      - legacy 兼容目标（DeepSeek thinking）：要求每个 item 都携带
        ``reasoning_text`` content；summary-only 的 item 回传后 provider 会报
        "reasoning_text must be passed back"。

    采用组级原子判定：一条历史消息的 replay 组不允许混合来源，任何一项不兼容
    则整组丢弃，让调用方回落到 ``reasoning_content`` 重建路径。
    """
    if not items:
        return False
    if allow_content:
        return all(_has_reasoning_text_content(item) for item in items)
    for item in items:
        item_id = str(item.get("id") or "")
        if item_id and not item_id.startswith("rs"):
            if model:
                logger.warning(
                    "[responses] dropping foreign reasoning replay item "
                    "(id prefix mismatch): model=%s id=%.32s",
                    model,
                    item_id,
                )
            return False
        if not (item.get("encrypted_content") or item.get("summary")):
            return False
    return True


def _reasoning_replay_items(
    message: dict[str, Any],
    *,
    allow_content: bool = False,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """读取 Host 写入 assistant 消息的原始 reasoning Output Item。

    只有与当前目标协议形状兼容的组才会返回；外来的历史组返回空列表，
    由调用方决定走旧式重建或直接省略。
    """
    candidates: Any = message.get("responses_output_items")
    if candidates is None:
        metadata = message.get("response_metadata")
        if isinstance(metadata, dict):
            candidates = metadata.get("output_items")
    if candidates is None:
        groups = message.get("responses_output_item_groups")
        if isinstance(groups, list) and groups:
            candidates = groups[0]
    if not isinstance(candidates, list):
        return []
    result: list[dict[str, Any]] = []
    for item in candidates:
        sanitized = _sanitize_reasoning_item(item, allow_content=allow_content)
        if sanitized is not None:
            result.append(sanitized)
    if result and not _replay_items_match_target(
        result,
        allow_content=allow_content,
        model=model,
    ):
        # 外来或残缺的历史组：整体作废，避免把上一个 Provider 的私有传输
        # 状态混入当前请求。
        return []
    return result


def _convert_user_content_to_responses(content: Any) -> Any:
    """user content → Responses API 兼容形态（对齐 DSH pi-ai context.ts）。

    DSH 语义：纯文本一律扁平化为字符串（`content.every(text) ? join('')`），
    只有含图片才用数组。chat 词汇的 [{"type": "text"}] 数组会被严格实现
    （Muse / OpenAI 规范）400 拒绝。

    ftre 的消息转换器（chat-completions 风格）可能产出：
    - 字符串（纯文本，Responses 原生支持，直接透传）
    - [{"type": "text", "text": ...}] → 纯文本，扁平化为字符串
    - [{"type": "image_url", ...}] 混合 → input_text / input_image 数组
    """
    if not isinstance(content, list):
        return content
    if all(isinstance(p, dict) and p.get("type") in ("text", "input_text") for p in content):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    converted: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type", "")
        if part_type in ("text", "input_text"):
            converted.append({"type": "input_text", "text": part.get("text", "")})
        elif part_type in ("image_url", "input_image"):
            image = part.get("image_url")
            url = image.get("url", "") if isinstance(image, dict) else str(image or "")
            image_part: dict[str, Any] = {"type": "input_image"}
            if url:
                image_part["image_url"] = url
            file_id = part.get("file_id") or part.get("image_file", {}).get("file_id")
            if file_id:
                image_part["file_id"] = str(file_id)
            detail = part.get("detail")
            if detail is None and isinstance(image, dict):
                detail = image.get("detail")
            if detail:
                image_part["detail"] = detail
            converted.append(image_part)
        else:
            # 未知 part（如插件扩展块）：按文本占位，保持 index 对齐
            converted.append({"type": "input_text", "text": ""})
    return converted


def _convert_assistant_content_to_responses(content: Any) -> Any:
    """assistant content → Responses API 兼容形态（对齐 DSH flattenText）。

    Responses API 的 assistant content 只接受字符串或 output_text 数组；
    chat 词汇的 [{"type": "text"}] 数组会被 400 拒绝（实测 Muse 与
    gpt-5.6-terra 均如此——ReAct 第二轮带 assistant 历史即触发）。
    ftre 的 to_openai_message 产出恰好是这种数组，必须扁平化。
    """
    if not isinstance(content, list):
        return content
    texts = [
        p.get("text", "")
        for p in content
        if isinstance(p, dict) and p.get("type") in ("text", "output_text")
    ]
    if len(texts) == len(content):
        return "".join(texts)
    # 含不可映射 part：逐 part 降级为 output_text（保持顺序与数量）
    return [
        {"type": "output_text", "text": p.get("text", "") if isinstance(p, dict) else ""}
        for p in content
    ]


def _convert_messages_to_responses_input(
    messages: list[dict],
    *,
    include_reasoning: bool = False,
    allow_legacy_reasoning_content: bool = False,
    model: str | None = None,
) -> tuple[str | None, list[dict]]:
    """Chat Completions messages → Responses API (instructions, input)。

    迁移自 completion.py，user content 增加 Responses 形态归一化：
      system       → instructions 参数（多条拼接）
      user         → {role: user, content}（列表 content 归一化为 input_text / input_image）
      assistant    → {role: assistant, content}（无 tool_calls 时）
      assistant+tc → 先 assistant content（如有），再若干 function_call 条目
      tool         → {type: function_call_output, call_id, output}
    """
    instructions: str | None = None
    input_items: list[dict] = []
    legacy_reasoning_count = 0
    omitted_reasoning_count = 0

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") in ("text", "input_text")
                )
            else:
                text = ""
            if text:
                instructions = text if not instructions else f"{instructions}\n\n{text}"

        elif role == "user":
            input_items.append({
                "role": "user",
                "content": _convert_user_content_to_responses(content),
            })

        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            reasoning_text = msg.get("reasoning_content")
            if include_reasoning and isinstance(reasoning_text, str) and reasoning_text:
                replay_items = _reasoning_replay_items(
                    msg,
                    allow_content=allow_legacy_reasoning_content,
                    model=model,
                )
                if replay_items:
                    input_items.extend(replay_items)
                elif allow_legacy_reasoning_content:
                    # OpenCode DeepSeek thinking 模式的窄兼容路径：旧会话没有
                    # Output Item 时，必须把 reasoning_text 重建为 provider 要求的
                    # reasoning_text content。GPT Responses 不走此分支。
                    legacy_reasoning_count += 1
                    input_items.append({
                        "type": "reasoning",
                        "id": f"rs_legacy_{legacy_reasoning_count}",
                        "summary": [],
                        "content": [{
                            "type": "reasoning_text",
                            "text": reasoning_text,
                        }],
                    })
                else:
                    # 旧会话只有 UI 用的 reasoning_content，没有 provider 返回的
                    # encrypted_content/summary。不能把 reasoning_text 伪造成
                    # Responses input 的 content 数组，否则 Console Go 会直接 400。
                    omitted_reasoning_count += 1
            content = _convert_assistant_content_to_responses(content)
            if tool_calls:
                if content:
                    input_items.append({"role": "assistant", "content": content})
                for tc in tool_calls:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    input_items.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", ""),
                    })
            else:
                # reasoning-only assistant 已经由上面的 reasoning item 表达，
                # 不再额外发送一个空 assistant message。
                if content or not (
                    include_reasoning
                    and isinstance(reasoning_text, str)
                    and reasoning_text
                ):
                    input_items.append({"role": "assistant", "content": content})

        elif role == "tool":
            call_id = msg.get("tool_call_id", "")
            if isinstance(content, str):
                output = content
            else:
                output = json.dumps(content, ensure_ascii=False)
            input_items.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            })

    if omitted_reasoning_count:
        logger.warning(
            "[responses] missing raw reasoning Output Item; "
            "omitting non-replayable legacy reasoning count=%d",
            omitted_reasoning_count,
        )
    return instructions, input_items


def _convert_tools_to_responses(tools: list[dict]) -> list[dict]:
    """Chat Completions tools → Responses API tools。

    迁移自 completion.py：
      Chat Completions: {type: function, function: {name, description, parameters}}
      Responses API:    {type: function, name, description, parameters}
    """
    result: list[dict] = []
    for tool in tools:
        if tool.get("type") == "function":
            fn = tool.get("function", {})
            result.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })
    return result

