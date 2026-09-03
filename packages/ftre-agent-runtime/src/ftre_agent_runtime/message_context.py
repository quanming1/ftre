"""Stateless helpers for reading and updating ``AgentState.context``."""
import uuid
from typing import Any

from ftre_agent.message import (
    ContentBlock,
    HintBlock,
    Msg,
    MsgName,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
    from_openai_message,
    to_openai_message,
)


def _msg_to_dicts(msg: Msg) -> list[dict]:
    """把一条 Agent ``Msg`` 展开为一组 Provider 可接收的 OpenAI 消息。

    为什么返回 ``list[dict]`` 而不是单个 ``dict``：
      FTRE 持久化时，一条 assistant ``Msg.content`` 可能聚合了多轮内容，
      例如：

      ``ThinkingBlock -> ToolCallBlock -> ToolResultBlock -> TextBlock``

      但 OpenAI 工具协议要求 ``ToolResultBlock`` 必须是独立的
      ``role="tool"`` 消息。因此上面一条 Msg 需要展开成：

      1. ``assistant(reasoning/content + tool_calls)``
      2. ``tool(tool_call_id + content)``
      3. ``assistant(content)``

    这里只负责“按 ToolResultBlock 切分消息序列”。单个 Block 如何
    转成 OpenAI 字段，由 ``to_openai_message()`` 负责。
    """
    # 最终展开后的 OpenAI 消息序列。一条 Msg 可能产生多条消息。
    messages: list[dict] = []

    # 暂存尚未输出的非 ToolResultBlock。它们将被合并为一条
    # assistant/user/system 消息，直到遇到 ToolResultBlock 才 flush。
    assistant_blocks: list[ContentBlock] = []

    # 兼容旧的消息存储方式：部分 Msg 没有 ThinkingBlock，而是把推理文本
    # 存在 metadata["reasoning_content"] 里。这个值只能注入第一段 assistant
    # 消息，否则一条 Msg 被切成多段后会重复发送同一段推理。
    metadata_reasoning = msg.metadata.get("reasoning_content")
    metadata_reasoning_used = False
    response_item_groups = msg.metadata.get("responses_output_item_groups")
    if not isinstance(response_item_groups, list):
        response_item_groups = []
    response_item_group_used = 0

    def flush_assistant() -> None:
        """把当前累积的非 tool-result blocks 输出为一条 OpenAI 消息。"""
        nonlocal metadata_reasoning_used, response_item_group_used

        # 没有待输出 block 时不生成空 assistant，避免在 tool 消息之前
        # 额外插入无意义的协议消息。
        if not assistant_blocks:
            return

        # 真正的 Block -> OpenAI 字段转换点：
        #   TextBlock       -> content
        #   ThinkingBlock   -> reasoning_content
        #   ToolCallBlock   -> tool_calls
        # role 沿用原 Msg.role，通常是 assistant，也可以是 user/system。
        result = to_openai_message(assistant_blocks, role=msg.role)

        # 旧格式 metadata reasoning 只注入第一个切分片段。如果 blocks 中已经
        # 有 ThinkingBlock，to_openai_message() 已生成非空 reasoning_content，
        # 此处不覆盖；如果只有默认空字符串，则用 metadata 中的真实推理补全。
        if not metadata_reasoning_used:
            if metadata_reasoning and not result.get("reasoning_content"):
                result["reasoning_content"] = metadata_reasoning
            metadata_reasoning_used = True

        if response_item_group_used < len(response_item_groups):
            group = response_item_groups[response_item_group_used]
            if isinstance(group, list) and group:
                result["responses_output_items"] = [
                    dict(item) for item in group if isinstance(item, dict)
                ]
            response_item_group_used += 1

        # 先加入结果，再清空原列表。clear() 不会影响 result，因为
        # to_openai_message() 已经构造了新的 dict/list。
        messages.append(result)
        assistant_blocks.clear()

    # 严格按 Msg.content 的原始顺序扫描，不重排 block。
    for block in msg.content:
        if isinstance(block, HintBlock):
            # HintBlock 在持久化结构中属于当前 reply，但在 Provider 边界必须表现为
            # 一条 user 消息。先结束它前面的 assistant 片段，再输出 hint，后续
            # thinking/text/tool_call 会重新组成下一段 assistant。
            flush_assistant()
            if isinstance(block.hint, str):
                messages.append({"role": "user", "content": block.hint})
            else:
                messages.append(to_openai_message([block], role="user"))
        elif isinstance(block, ToolResultBlock):
            # tool result 前面累积的 reasoning/text/tool_call 必须先输出，
            # 从而保证 OpenAI 协议中 assistant(tool_calls) 在 tool(result) 之前。
            flush_assistant()

            # 每个 ToolResultBlock 单独转成一条 role="tool" 消息。
            # tool_call_id 由 ToolResultBlock.id 生成，用来和前面的 tool_calls 配对。
            messages.append(to_openai_message([block], role="tool"))
        else:
            # 非 ToolResultBlock 保持原顺序累积，稍后合并成一条消息。
            assistant_blocks.append(block)

    # 循环结束后，Msg 尾部可能还有没遇到 tool result 的 blocks，
    # 例如最终 TextBlock，或尚未获得结果的 ToolCallBlock。
    flush_assistant()

    # 如果 Msg.content 完全为空，仍保留一条空消息以兼容现有语义。
    # 注意：这里不判断具体 Provider 是否接受空 assistant。真正发请求前，
    # completion._normalize_chat_messages() 会再过滤无 content/tool_calls 的无效消息。
    if not messages:
        result = to_openai_message([], role=msg.role)

        # 无 content blocks 但存在旧 metadata reasoning 时，仍把它带到中间结果。
        # 它之后是否会被 Provider 边界保留，由 normalization 规则决定。
        if metadata_reasoning:
            result["reasoning_content"] = metadata_reasoning
        messages.append(result)
    return messages


class MessageContext:
    """Operate on a caller-owned ``list[Msg]`` without storing state itself."""

    @staticmethod
    def messages(context: list[Msg]) -> list[dict]:
        """把完整 Agent 上下文转成按时序排列的 OpenAI 消息列表。

        ``context`` 中的每条 Msg 依次交给 ``_msg_to_dicts()``；因为一条 Msg
        可能展开为多条 OpenAI 消息，这里用双层遍历将结果打平。

        该方法不添加 system prompt；需要 system prompt 时调用
        ``get_messages()``。
        """
        return [item for message in context for item in _msg_to_dicts(message)]

    @staticmethod
    def get_messages(context: list[Msg], system_prompt: str = "") -> list[dict]:
        """在已转换的历史消息前面插入一条 system 消息。"""
        return [
            {"role": "system", "content": system_prompt},
            *MessageContext.messages(context),
        ]

    @staticmethod
    def add_user(context: list[Msg], content: str) -> None:
        context.append(
            Msg(name=MsgName.DEFAULT, content=[TextBlock(text=content)], role="user")
        )

    @staticmethod
    def append_reply_blocks(
        context: list[Msg],
        message_id: str,
        blocks: list[ContentBlock],
    ) -> None:
        """把内容块追加到 ``message_id`` 对应的 assistant 消息。

        一次 ``agent.run()`` 可以产生多个 AssistantMsg。一个 Reasoning 及其
        Acting 使用同一个 message_id；下一次 Reasoning 若前面插入了正式
        UserMessage，则由 Runner 分配新的 message_id。Provider 仍由
        ``_msg_to_dicts()`` 负责 ToolResultBlock / HintBlock 的协议展开。

        通常当前 AssistantMsg 位于尾部；如果 Hook 只插入了 system/assistant
        上下文，仍回到同一个已有 message_id 追加，避免无边界的重复 Msg.id。
        正式 UserMessage 已在 Runner 中先触发 message_id 旋转，因此不会复用旧消息。
        空 blocks 不创建空 assistant，避免产生无 content/tool_calls 的非法请求。
        """
        if not blocks:
            return
        for message in reversed(context):
            if message.role == "assistant" and message.id == message_id:
                message.content.extend(blocks)
                return
        context.append(
            Msg(
                id=message_id,
                name=MsgName.DEFAULT,
                content=blocks,
                role="assistant",
            )
        )

    @staticmethod
    def add_tool_result(
        context: list[Msg],
        *,
        message_id: str,
        tool_call_id: str,
        name: str,
        content: str,
        state: ToolResultState = ToolResultState.SUCCESS,
    ) -> None:
        """把工具结果追加到其所属 AssistantMsg。

        ``message_id`` 和 ``name`` 必须显式提供：ToolResult 必须回到产生
        ToolCall 的 AssistantMsg，不能依赖整次 run 的 reply_id 猜测。
        """
        block = ToolResultBlock(
            id=tool_call_id,
            name=name,
            output=content,
            state=state,
        )
        MessageContext.append_reply_blocks(context, message_id, [block])

    @staticmethod
    def add_raw(context: list[Msg], message: Any) -> None:
        if isinstance(message, Msg):
            context.append(message)
            return
        if isinstance(message, dict):
            blocks = from_openai_message(message)
            role = message.get("role", "user")
            if role not in ("user", "assistant", "system"):
                role = "assistant"
            message_id = message.get("id")
            if message_id and any(item.id == message_id for item in context):
                return
            metadata = message.get("metadata")
            metadata = dict(metadata) if isinstance(metadata, dict) else {}
            # Responses 原始 Output Item 由 Host 通过普通消息字段传入；
            # 它只进入 Runtime metadata，不会被误当成可见 content。
            if isinstance(message.get("responses_output_items"), list):
                metadata["responses_output_item_groups"] = [
                    [dict(item) for item in message["responses_output_items"] if isinstance(item, dict)]
                ]
            elif isinstance(message.get("response_metadata"), dict):
                output_items = message["response_metadata"].get("output_items")
                if isinstance(output_items, list):
                    metadata["responses_output_item_groups"] = [
                        [dict(item) for item in output_items if isinstance(item, dict)]
                    ]
            context.append(
                Msg(
                    name=MsgName.DEFAULT,
                    content=blocks,
                    role=role,
                    id=message_id or uuid.uuid4().hex[:16],
                    metadata=metadata,
                )
            )
            return
        context.append(message)

    @staticmethod
    def set_tool_call_state(
        context: list[Msg], tool_call_id: str, state: ToolCallState
    ) -> bool:
        """把 context 里指定 tool_call 的 ToolCallBlock 状态改为 ``state``。

        权限流程用它标记工具调用的生命周期：挂起时置 ASKING，用户确认后置
        ALLOWED，拒绝或了结后置 FINISHED。返回是否找到并修改了对应 block。
        """
        for message in context:
            for block in message.content:
                if isinstance(block, ToolCallBlock) and block.id == tool_call_id:
                    block.state = state
                    return True
        return False

    @staticmethod
    def tool_calls_in_state(
        context: list[Msg], state: ToolCallState
    ) -> list[ToolCallBlock]:
        """收集 context 里处于指定状态的全部 ToolCallBlock（保持出现顺序）。

        用于恢复阶段：读出仍 ASKING 的调用判断是否还需暂停，或读出 ALLOWED
        的调用交给 resume_execute 整批执行。
        """
        found: list[ToolCallBlock] = []
        for message in context:
            for block in message.content:
                if isinstance(block, ToolCallBlock) and block.state == state:
                    found.append(block)
        return found

    @staticmethod
    def clear(context: list[Msg]) -> None:
        context.clear()

    @staticmethod
    def set_messages(context: list[Msg], messages: list[dict | Msg]) -> None:
        context.clear()
        for message in messages:
            MessageContext.add_raw(context, message)
