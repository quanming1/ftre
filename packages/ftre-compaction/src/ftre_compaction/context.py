"""Compaction Plugin 的 ContextView 构建。

SessionService 只保存完整 Msg；本模块把 compact marker 解释为一次 LLM
请求的内存视图，不修改任何持久化消息。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ftre_agent.message import Msg, MsgName, TextBlock, ToolResultBlock

TRIMMED_TOOL_RESULT_PLACEHOLDER = "[已压缩裁剪：原始工具输出不再可见]"
LEGACY_TRIMMED_TOOL_RESULT_PLACEHOLDER = "[工具输出已压缩]"


def build_context_view(messages: Iterable[Msg]) -> list[Msg]:
    """按最新 compact marker 生成一份可交给 LLM 的深拷贝视图。"""

    snapshot = [message.model_copy(deep=True) for message in messages]
    trim_ids: set[str] = set()
    for index, message in enumerate(snapshot):
        if message.name != MsgName.COMPACT_FAST:
            continue
        compact_meta = _compact_metadata(message)
        ids = compact_meta.get("tool_result_ids") or []
        if isinstance(ids, list) and ids:
            trim_ids.update(str(item) for item in ids if item)
            continue
        count = _nonnegative_int(compact_meta.get("tool_results"))
        if count:
            eligible = [
                block.id
                for previous in snapshot[:index]
                for block in previous.content
                if isinstance(block, ToolResultBlock) and block.id not in trim_ids
            ]
            trim_ids.update(eligible[:count])

    anchor_index = -1
    for index, message in enumerate(snapshot):
        if message.role == "user" and message.name == MsgName.COMPACT:
            anchor_index = index

    if anchor_index >= 0:
        compact = snapshot[anchor_index]
        compact_meta = _compact_metadata(compact)
        through_id = str(compact_meta.get("through_message_id") or "")
        through_index = next(
            (
                index
                for index, item in enumerate(snapshot)
                if item.id == through_id
            ),
            -1,
        )
        if through_index >= 0:
            view = [
                compact,
                *(
                    item
                    for item in snapshot[through_index + 1 :]
                    if item.id != compact.id
                ),
            ]
        else:
            view = [compact, *snapshot[anchor_index + 1 :]]
    else:
        view = snapshot

    if trim_ids:
        for message in view:
            for block in message.content:
                if isinstance(block, ToolResultBlock) and block.id in trim_ids:
                    block.output = [TextBlock(text=TRIMMED_TOOL_RESULT_PLACEHOLDER)]
                    block.metadata = dict(block.metadata or {})

    return view


def _compact_metadata(message: Msg) -> Mapping[str, object]:
    value = message.metadata.get("context_compact")
    return value if isinstance(value, Mapping) else {}


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "LEGACY_TRIMMED_TOOL_RESULT_PLACEHOLDER",
    "TRIMMED_TOOL_RESULT_PLACEHOLDER",
    "build_context_view",
]
