"""类型化消息内容块 + Msg 实体。

层次 A: ContentBlock + 状态机 + OpenAI dict 转换器
层次 B: Msg 实体 + 工厂函数
"""
from ._block import (
    # 数据源
    Base64Source,
    # 类型别名
    ContentBlock,
    ContentBlockTypes,
    DataBlock,
    ExtensionBlock,
    HintBlock,
    # 内容块（含未来扩展保留容器）
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    # 状态机
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
    URLSource,
)
from ._convert import (
    from_openai_message,
    from_openai_part,
    to_openai_message,
    to_openai_part,
)
from ._msg import (
    AssistantMsg,
    Msg,
    MsgName,
    MsgToken,
    SystemMsg,
    TokenUsage,
    UserMsg,
)

__all__ = [
    "AssistantMsg",
    "Base64Source",
    "ContentBlock",
    "ContentBlockTypes",
    "DataBlock",
    "ExtensionBlock",
    "HintBlock",
    "Msg",
    "MsgName",
    "MsgToken",
    "SystemMsg",
    "TextBlock",
    "ThinkingBlock",
    # 层次 B
    "TokenUsage",
    "ToolCallBlock",
    "ToolCallState",
    "ToolResultBlock",
    "ToolResultState",
    "URLSource",
    "UserMsg",
    "from_openai_message",
    "from_openai_part",
    "to_openai_message",
    "to_openai_part",
]
