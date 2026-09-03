"""ContentBlock ↔ OpenAI dict 双向转换器。

ftre 支持两种「工具调用」边界表示：
  1. content part 风格：``{type:"toolCall", id, name, arguments:dict}``
  2. OpenAI 标准消息字段：``tool_calls: [{id, type:"function",
     function:{name, arguments:str_JSON}}]`` —— 出现在 memory / LLM 调用里

两个视角的转换函数:
  - ``to_openai_part`` / ``from_openai_part``：单个 content part ↔ Block
  - ``to_openai_message`` / ``from_openai_message``：整条消息 ↔ Block 列表
    （给 LLM 用，ToolCallBlock → tool_calls 字段 + arguments str JSON；
     ToolResultBlock → 独立 role=tool 消息）
"""
from __future__ import annotations

import base64
import json
from typing import Any

from ._block import (
    Base64Source,
    ContentBlock,
    DataBlock,
    HintBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
    URLSource,
)

# ══════════════════════════════════════════════════════════════════
# 单个 content part ↔ Block
# ══════════════════════════════════════════════════════════════════

def from_openai_part(part: dict) -> ContentBlock:
    """把单个 OpenAI content part dict 转成 Block。

    支持的 part type:
      - ``text``       → TextBlock
      - ``thinking``   → ThinkingBlock（ftre content part 里的推理）
      - ``image_url``  → DataBlock(URLSource)
      - ``image_file`` → DataBlock(Base64Source，读文件转 base64）
      - ``toolCall``   → ToolCallBlock（ftre 事件风格，arguments 为 dict）

    未知 type 降级为 TextBlock（text=str(part)），不抛异常。
    """
    ptype = part.get("type", "text")

    if ptype == "text":
        return TextBlock(text=part.get("text", ""))

    if ptype == "thinking":
        return ThinkingBlock(thinking=part.get("thinking", ""))

    if ptype == "image_url":
        url_info = part.get("image_url", {})
        url = url_info.get("url", "") if isinstance(url_info, dict) else str(url_info)
        # data URL 形如 data:image/png;base64,xxxx —— 拆出 media_type 与 base64
        media_type = "image/png"
        if url.startswith("data:"):
            header, _, payload = url.partition(",")
            # header: data:image/png;base64
            mt = header.split(";")[0].split(":", 1)[-1]
            if mt:
                media_type = mt
            if ";base64" in header:
                return DataBlock(
                    source=Base64Source(data=payload, media_type=media_type),
                    name=part.get("name"),
                )
        return DataBlock(
            source=URLSource(url=url, media_type=media_type),
            name=part.get("name"),
        )

    if ptype == "image_file":
        # ftre UserMessageEvent 里未转换的 image_file part（读文件转 base64）
        path = part.get("path", "")
        mime = part.get("mime_type", "image/png")
        try:
            with open(path, "rb") as f:
                raw = f.read()
            b64 = base64.b64encode(raw).decode("ascii")
            return DataBlock(
                source=Base64Source(data=b64, media_type=mime),
                name=part.get("name"),
            )
        except OSError:
            return TextBlock(text=f"[图片加载失败: {path}]")

    if ptype == "toolCall":
        return ToolCallBlock(
            id=part.get("id", ""),
            name=part.get("name", ""),
            arguments=part.get("arguments") or {},
            state=ToolCallState.PENDING,
        )

    # 未知 part 降级为文本，避免抛异常打断流式
    return TextBlock(text=json.dumps(part, ensure_ascii=False))


def _tool_result_content(output: str | list) -> str:
    """Flatten textual tool output without exposing internal block JSON to the LLM."""
    if isinstance(output, str):
        return output
    chunks: list[str] = []
    for item in output:
        if isinstance(item, TextBlock):
            chunks.append(item.text)
        elif isinstance(item, dict) and item.get("type") == "text":
            chunks.append(str(item.get("text", "")))
        else:
            value = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            chunks.append(json.dumps(value, ensure_ascii=False))
    return "\n".join(chunk for chunk in chunks if chunk)


def to_openai_part(block: ContentBlock) -> dict:
    """把 Block 转成单个 OpenAI content part dict（ftre 事件风格）。

    注意：``ToolResultBlock`` 不对应 content part（它是独立 role=tool 消息），
    调用方不应把它传进来；若误传，降级为 text part。

    ToolCallBlock → ``{type:"toolCall", id, name, arguments:dict}``（camelCase，
    贴合 ftre 现有 content part 格式，arguments 保持 dict）。
    """
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}

    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": block.thinking}

    if isinstance(block, DataBlock):
        src = block.source
        if isinstance(src, Base64Source):
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{src.media_type};base64,{src.data}"},
            }
        # URLSource
        return {
            "type": "image_url",
            "image_url": {"url": src.url},
        }

    if isinstance(block, HintBlock):
        # hint 传给 LLM 时作为 user 文本；多模态 hint 取文本拼接
        hint = block.hint
        if isinstance(hint, list):
            texts = [p.get("text", "") for p in hint if isinstance(p, dict) and p.get("type") == "text"]
            hint = "\n".join(texts)
        return {"type": "text", "text": str(hint)}

    if isinstance(block, ToolCallBlock):
        return {
            "type": "toolCall",
            "id": block.id,
            "name": block.name,
            "arguments": block.arguments,
        }

    if isinstance(block, ToolResultBlock):
        # ToolResultBlock 不属于 content part，降级为文本
        return {"type": "text", "text": _tool_result_content(block.output)}

    return {"type": "text", "text": str(block)}


# ══════════════════════════════════════════════════════════════════
# 整条 OpenAI 消息 ↔ Block 列表
# ══════════════════════════════════════════════════════════════════

def from_openai_message(msg: dict) -> list[ContentBlock]:
    """把一条 OpenAI 消息转成 Block 列表。

    处理:
      - ``role=="tool"`` → [ToolResultBlock]（id=tool_call_id, output=content）
      - 其他 role:
          * ``content`` 为 list → 逐 part from_openai_part
          * ``content`` 为 str/None → TextBlock（空串时返回 []）
          * ``reasoning_content`` → 追加 ThinkingBlock
          * ``tool_calls`` 字段（OpenAI 标准）→ 追加 ToolCallBlock
            （arguments 从 str JSON 解析为 dict；解析失败用空 dict）
    """
    role = msg.get("role", "user")

    # 工具结果消息
    if role == "tool":
        return [
            ToolResultBlock(
                id=msg.get("tool_call_id", ""),
                name=msg.get("name", ""),
                output=msg.get("content", ""),
                state=ToolResultState.SUCCESS,
            )
        ]

    blocks: list[ContentBlock] = []

    # content parts
    content = msg.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                blocks.append(from_openai_part(part))
            else:
                blocks.append(TextBlock(text=str(part)))
    elif isinstance(content, str) and content:
        blocks.append(TextBlock(text=content))

    # reasoning_content（DeepSeek 等网关的独立推理字段）
    reasoning = msg.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        blocks.append(ThinkingBlock(thinking=reasoning))

    # tool_calls 字段（OpenAI 标准，arguments 是 str JSON）
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
            args_str = fn.get("arguments", "")
            try:
                args = json.loads(args_str) if args_str else {}
            except (json.JSONDecodeError, TypeError):
                args = {}
            blocks.append(
                ToolCallBlock(
                    id=tc.get("id", ""),
                    name=fn.get("name", ""),
                    arguments=args,
                    state=ToolCallState.PENDING,
                )
            )

    return blocks


def to_openai_message(
    blocks: list[ContentBlock],
    role: str | None = None,
) -> dict:
    """把 Block 列表转成一条 OpenAI 消息。

    两种消息形态（按 blocks 内容自动判断，除非 role 显式指定）:
      1. 含 ToolResultBlock → ``{role:"tool", tool_call_id, content}``（取首个
         ToolResultBlock；ftre 工具结果是独立消息，一条一结果）
      2. 否则 → ``{role:"assistant", content:[parts], tool_calls:[...],
         reasoning_content:str}``：
         * TextBlock/DataBlock/HintBlock → content parts
         * ThinkingBlock 只写 reasoning_content（OpenAI content 不接受 thinking part）
         * assistant 没有 ThinkingBlock 时 reasoning_content 为空字符串
         * ToolCallBlock → tool_calls 字段（arguments 序列化为 str JSON）
    """
    # 形态 1：工具结果消息
    if role == "tool" or (role is None and blocks and isinstance(blocks[0], ToolResultBlock)):
        for b in blocks:
            if isinstance(b, ToolResultBlock):
                return {
                    "role": "tool",
                    "tool_call_id": b.id,
                    "content": _tool_result_content(b.output),
                }
        # role=tool 但无 ToolResultBlock —— 返回空 tool 消息
        return {"role": "tool", "tool_call_id": "", "content": ""}

    # 形态 2：assistant 消息
    content_parts: list[dict] = []
    tool_calls: list[dict] = []
    reasoning_parts: list[str] = []

    for block in blocks:
        if isinstance(block, ToolCallBlock):
            tool_calls.append({
                "id": block.id,
                "type": "function",
                "function": {
                    "name": block.name,
                    "arguments": json.dumps(block.arguments, ensure_ascii=False),
                },
            })
        elif isinstance(block, ThinkingBlock):
            reasoning_parts.append(block.thinking)
        elif isinstance(block, ToolResultBlock):
            # assistant 消息里夹带 ToolResultBlock —— 降级为 text part
            content_parts.append({"type": "text", "text": _tool_result_content(block.output)})
        else:
            content_parts.append(to_openai_part(block))

    msg: dict[str, Any] = {
        "role": role or "assistant",
        "content": content_parts if content_parts else "",
    }
    # reasoning_content 是 assistant 消息的稳定字段：有推理时写入
    # 完整内容，无推理时也显式保留为 ""。user/system 不携带该字段。
    if msg["role"] == "assistant":
        msg["reasoning_content"] = "\n".join(reasoning_parts)
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg
