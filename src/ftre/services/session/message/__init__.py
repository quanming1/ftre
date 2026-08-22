"""Message 层：消息内容处理（LLM 适配），与 Session 存储无关。

- converter:     持久化 Msg 快照 → provider（OpenAI）消息格式
- token_counter: 字符级 token 粗估
- multimodal:    多模态内容构建/归一化
"""

# These two functions are the stable message-conversion contract consumed by
# optional packages such as ``ftre-compaction``.  Keep the implementation
# modules private so consumers do not couple to Session internals.
from .converter import to_openai
from .token_counter import estimate_messages_tokens

__all__ = ["estimate_messages_tokens", "to_openai"]
