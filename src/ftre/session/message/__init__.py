"""Message 层：消息内容处理（LLM 适配），与 Session 存储无关。

- converter:     持久化 Msg 快照 → provider（OpenAI）消息格式
- token_counter: 字符级 token 粗估
- multimodal:    多模态内容构建/归一化
"""
