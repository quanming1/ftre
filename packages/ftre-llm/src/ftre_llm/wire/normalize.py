"""wire/normalize.py —— 协议共用的消息归一化与 usage 映射（PRD-B2 FR6）。

从 completion.py 迁出，两协议适配器共用。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _normalize_chat_messages(
    messages: list[dict],
    *,
    preserve_reasoning_only: bool = False,
) -> list[dict]:
    """复制并规范化 Chat Completions 消息，不污染调用方的 memory/history。

    遵循 OpenAI-compatible 工具调用语义：
    - assistant 内容为空（包括 reasoning-only）时统一设 content=""
    - 不使用占位符字符串：模型会将其作为可见文本 echo 回来，
      导致 runner 误判为正常完成而停止
    """
    def normalize_assistant_content(message: dict) -> None:
        content = message.get("content")
        is_empty = content is None or content == [] or (
            isinstance(content, str) and not content.strip()
        )
        if is_empty:
            # OpenAI-compatible 网关对 nullable content 的兼容性并不一致。
            # 统一使用空字符串，避免最终 JSON 请求中出现 content: null；
            # 没有 tool_calls 的纯空 assistant 仍会在下方被整条过滤。
            message["content"] = ""

    def has_assistant_payload(message: dict) -> bool:
        # OpenAI-compatible providers require visible content or tool_calls.
        # Responses thinking mode 是例外：上一轮 reasoning_text 必须作为
        # reasoning item 回传，因此允许调用方保留 reasoning-only assistant。
        return bool(message.get("content")) or (
            preserve_reasoning_only and bool(message.get("reasoning_content"))
        )

    # OpenAI-compatible provider 要求 assistant.tool_calls 后紧跟、且只紧跟
    # 每个 call 一条对应 tool result。持久化快照在中断或并发工具时可能不完整，
    # 这里不重排用户历史，只移除无效的协议片段以避免整个请求被 400 拒绝。
    normalized: list[dict] = []
    dropped_orphan_results = 0
    stripped_incomplete_calls = 0
    index = 0
    while index < len(messages):
        current = dict(messages[index])
        role = current.get("role")

        if role == "tool":
            # 只有紧跟对应 tool_calls 的分支才允许 result 通过。
            dropped_orphan_results += 1
            index += 1
            continue

        if role != "assistant":
            normalized.append(current)
            index += 1
            continue

        normalize_assistant_content(current)
        tool_calls = current.get("tool_calls") or []
        if not tool_calls:
            if has_assistant_payload(current):
                normalized.append(current)
            index += 1
            continue

        call_ids = [call.get("id") for call in tool_calls if isinstance(call, dict)]
        valid_call_ids = (
            len(call_ids) == len(tool_calls)
            and all(isinstance(call_id, str) and call_id for call_id in call_ids)
            and len(set(call_ids)) == len(call_ids)
        )
        following: list[dict] = []
        cursor = index + 1
        while cursor < len(messages) and messages[cursor].get("role") == "tool":
            following.append(dict(messages[cursor]))
            cursor += 1

        result_ids = [result.get("tool_call_id") for result in following]
        complete_pair = (
            valid_call_ids
            and len(following) == len(call_ids)
            and set(result_ids) == set(call_ids)
            and all(isinstance(result_id, str) and result_id for result_id in result_ids)
        )
        if complete_pair:
            normalized.append(current)
            normalized.extend(following)
            index = cursor
            continue

        # 没有结果、结果不全，或被其他消息隔开：保留可读 assistant 文本，
        # 解除其对 tool result 的协议约束；后续 tool result 会作为 orphan 丢弃。
        current.pop("tool_calls", None)
        stripped_incomplete_calls += 1
        if has_assistant_payload(current):
            normalized.append(current)
        index += 1

    if stripped_incomplete_calls or dropped_orphan_results:
        logger.warning(
            "[completion] normalized invalid tool protocol: stripped_calls=%d orphan_results=%d",
            stripped_incomplete_calls,
            dropped_orphan_results,
        )
    return normalized


def normalize_usage(usage: Any) -> dict | None:
    """把 SDK usage 对象规范化成统一的 token 字段。

    Chat Completions 使用 ``prompt_tokens`` / ``completion_tokens``，而
    Responses API 的标准字段是 ``input_tokens`` / ``output_tokens``。
    下游 Runner 只消费前一组规范字段，因此在协议边界补齐别名，保留原始
    字段及明细以便追踪系统使用。
    """
    if usage is None:
        return None
    if isinstance(usage, dict):
        normalized = dict(usage)
    elif hasattr(usage, "model_dump"):
        normalized = usage.model_dump(exclude_none=True)
    elif hasattr(usage, "__dict__"):
        normalized = {
            key: value
            for key, value in vars(usage).items()
            if value is not None and not key.startswith("_")
        }
    else:
        return None

    aliases = {
        "prompt_tokens": "input_tokens",
        "completion_tokens": "output_tokens",
    }
    for canonical, responses_field in aliases.items():
        if canonical not in normalized and responses_field in normalized:
            normalized[canonical] = normalized[responses_field]
    return normalized

