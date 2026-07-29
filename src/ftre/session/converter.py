"""Persisted Msg snapshots → provider message dictionaries.

The database stores complete ``Msg`` objects. Streaming ``AgentStreamEvent``
objects never enter this layer; they are a transport/trace concern only.
"""
from __future__ import annotations

import json
from typing import Any

from ftre_agent_core.message import (
    DataBlock,
    HintBlock,
    Msg,
    MsgName,
    TextBlock,
    ToolResultBlock,
    to_openai_message,
    to_openai_part,
)

from ftre.session.manager import MessageModel
from ftre.session.multimodal import IMAGE_OMITTED_NOTICE


def _as_msg(record: MessageModel | Msg | dict[str, Any]) -> Msg:
    if isinstance(record, Msg):
        return record
    fields = {
        key: record.get(key)
        for key in (
            "id",
            "name",
            "role",
            "content",
            "metadata",
            "created_at",
            "token",
            "finished_at",
            "finished_reason",
            "structured_output",
            "error",
        )
        if key in record
    }
    return Msg.model_validate(fields)


def _tool_result_message(block: ToolResultBlock) -> dict[str, Any]:
    output = block.output
    if isinstance(output, str):
        content = output
    elif all(
        isinstance(item, TextBlock)
        or (isinstance(item, dict) and item.get("type") == "text")
        for item in output
    ):
        content = "".join(
            item.text if isinstance(item, TextBlock) else str(item.get("text", ""))
            for item in output
        )
    else:
        content = json.dumps(
            [
                item.model_dump(mode="json")
                if hasattr(item, "model_dump")
                else item
                for item in output
            ],
            ensure_ascii=False,
        )
    return {
        "role": "tool",
        "tool_call_id": block.id,
        "content": content,
    }


def _hint_message(block: HintBlock, *, include_images: bool) -> dict[str, Any]:
    if isinstance(block.hint, str):
        return {"role": "user", "content": block.hint}

    parts: list[dict[str, Any]] = []
    omitted_image = False
    for item in block.hint:
        if isinstance(item, TextBlock):
            parts.append(to_openai_part(item))
        elif isinstance(item, DataBlock):
            if include_images:
                parts.append(to_openai_part(item))
            else:
                omitted_image = True
        elif isinstance(item, dict):
            if item.get("type") == "data" and not include_images:
                omitted_image = True
            else:
                parts.append(item)
        else:
            parts.append({"type": "text", "text": str(item)})
    if omitted_image:
        parts.append({"type": "text", "text": IMAGE_OMITTED_NOTICE})
    return {"role": "user", "content": parts or ""}


def _regular_message(msg: Msg, *, include_images: bool) -> dict[str, Any] | None:
    blocks = []
    omitted_image = False
    for block in msg.content:
        if isinstance(block, DataBlock) and not include_images:
            omitted_image = True
            continue
        blocks.append(block)
    if omitted_image:
        blocks.append(TextBlock(text=IMAGE_OMITTED_NOTICE))

    provider_message = to_openai_message(blocks, role=msg.role)
    if (
        provider_message.get("content")
        or provider_message.get("tool_calls")
        or provider_message.get("reasoning_content")
    ):
        return provider_message
    return None


def _assistant_messages(msg: Msg, *, include_images: bool) -> list[dict[str, Any]]:
    """Split one aggregate assistant Msg at tool-result/hint boundaries."""
    output: list[dict[str, Any]] = []
    pending = []

    def flush() -> None:
        if not pending:
            return
        provider_message = to_openai_message(list(pending), role="assistant")
        if (
            provider_message.get("content")
            or provider_message.get("tool_calls")
            or provider_message.get("reasoning_content")
        ):
            output.append(provider_message)
        pending.clear()

    for block in msg.content:
        if isinstance(block, ToolResultBlock):
            flush()
            output.append(_tool_result_message(block))
        elif isinstance(block, HintBlock):
            flush()
            output.append(_hint_message(block, include_images=include_images))
        elif isinstance(block, DataBlock) and not include_images:
            pending.append(TextBlock(text=IMAGE_OMITTED_NOTICE))
        else:
            pending.append(block)
    flush()
    return output


def to_openai(
    records: list[MessageModel | Msg | dict[str, Any]],
    *,
    config: dict | None = None,
) -> list[dict[str, Any]]:
    """Convert persisted Msg snapshots to OpenAI Chat Completions messages.

    上下文裁剪（只保留最后一条 compact Msg 及其后的消息）已由
    ``SessionManager.get_context_messages()`` 完成，本函数只负责把每条
    Msg 转成 provider 消息，不再做二次 clear。
    """
    llm_config = (config or {}).get("llm") or {}
    include_images = bool(llm_config.get("vision", False))
    messages: list[dict[str, Any]] = []

    for record in records:
        msg = _as_msg(record)
        # compact 摘要 Msg：作为 user 消息发给 LLM，正文带前缀标记。
        if msg.name == MsgName.COMPACT:
            summary = msg.get_text_content() or ""
            if summary:
                messages.append(
                    {"role": "user", "content": f"[历史上下文摘要]\n{summary}"}
                )
            continue

        if msg.role == "assistant":
            messages.extend(_assistant_messages(msg, include_images=include_images))
        else:
            provider_message = _regular_message(msg, include_images=include_images)
            if provider_message is not None:
                messages.append(provider_message)
    return messages
