# ftre-llm

统一的 LLM Service 与 OpenAI Chat Completions/Responses 协议适配器。

流输出协议由本包自己的 `ftre_llm.events` 持有和维护。本包不依赖已退休的
Agent Core，也不打包旧路径兼容桥。

本包只负责一次 LLM 调用、协议注册和 StreamChunk 输出，不负责 Agent、Session、队列、
压缩、重试或 fallback。`contracts.py` 中只有一个公开适配器契约 `LlmAdapter`；
`base.py` 只是 OpenAI 适配器的共享实现骨架，不再定义第二个契约。

Host 通过 `ftre.services.llm.plugin` 创建并发布 `llm` Service。随后由本包的
`ftre_llm.adapters.plugin` Provider Plugin 注入该 Service，并通过
`register_adapter("completions", ...)` / `register_adapter("responses", ...)`
注册具体协议。卸载 Provider Plugin 会撤销两个路由，但不会销毁 `llm` Service。
