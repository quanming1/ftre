"""
title_gen — 首条消息自动生成会话标题

通过 before_messages_build hook 判断是否首条消息：
- events 为空（DB 里还没有任何事件）
- session 没有 title

满足条件时异步起线程调 LLM 生成标题并落库。
前端有 sessions 列表轮询，新 title 自然会刷新出来。

可配置项（通过 ~/.ftre/config.json 的 plugins 数组传入）：
- system_prompt: 标题生成的 system prompt
- input_truncate: 用户消息截断长度（默认 1000）
- max_chars: 标题最大字符数（默认 40）
"""
import asyncio
import logging
import threading

from ftre.plugin import Plugin, BEFORE_MESSAGES_BUILD

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "你是一个标题生成器。给定一段用户消息，给出一个20字以内的简短标题，"
    "概括用户的意图。只输出标题本身，不要引号、不要标点、不要前缀如『标题：』。"
)
DEFAULT_INPUT_TRUNCATE = 1000
DEFAULT_MAX_CHARS = 40


class TitleGenPlugin(Plugin):
    name = "title_gen"
    version = "1.0.0"

    def setup(self) -> None:
        cfg = self.api.config or {}
        self._system_prompt = cfg.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        self._input_truncate = cfg.get("input_truncate", DEFAULT_INPUT_TRUNCATE)
        self._max_chars = cfg.get("max_chars", DEFAULT_MAX_CHARS)
        self.api.register_hook(BEFORE_MESSAGES_BUILD, self._on_build)
        logger.info(
            "[title_gen] 插件已就绪，已注册 before_messages_build hook "
            f"(input_truncate={self._input_truncate}, max_chars={self._max_chars})"
        )

    async def _on_build(self, ctx):
        """before_messages_build hook：首条消息时异步生成标题"""
        session_id = ctx.session_id

        # 判断是否首条：events 为空 = DB 里还没有任何事件
        if ctx.events:
            logger.debug(
                f"[title_gen] 跳过：非首条消息 (session={session_id}, "
                f"已有 {len(ctx.events)} 条事件)"
            )
            return ctx

        inbound_data = ctx.inbound_data
        config = ctx.config
        event_loop = self.api.event_loop

        # event_loop 缺失会导致后续 run_coroutine_threadsafe 静默失败，提前显式拦截
        if event_loop is None:
            logger.warning(
                f"[title_gen] 跳过：event_loop 未注入，无法跨线程调度 "
                f"(session={session_id})"
            )
            return ctx

        # 注意：_on_build 运行在主事件循环线程上（await trigger 调用）。
        # 这里【不能】用 run_coroutine_threadsafe(...).result() 去查 session——那会把协程
        # 排回当前正阻塞的事件循环，造成自我等待死锁（表现为 5s TimeoutError，并卡死整个
        # 事件循环）。因此 session 是否已有标题的检查移到 worker 线程里做。

        # 提取文本
        text = self._extract_text(inbound_data)
        if not text:
            logger.warning(
                f"[title_gen] 跳过：未能从 inbound_data 提取到文本 "
                f"(session={session_id}, content_type={type(inbound_data.get('content')).__name__})"
            )
            return ctx
        text = text[: self._input_truncate]

        logger.info(
            f"[title_gen] 满足首条条件，开始异步生成标题 "
            f"(session={session_id}, 文本长度={len(text)})"
        )
        # 异步生成标题（不阻塞主流程）
        self._spawn_title_generation(session_id, text, config, event_loop)
        return ctx

    def _spawn_title_generation(
        self, session_id: str, text: str, config, event_loop
    ) -> None:
        """起守护线程跑 LLM 生成标题"""

        def worker() -> None:
            # session 是否已有标题的检查放在 worker 线程里：这里不是事件循环线程，
            # run_coroutine_threadsafe(...).result() 可以安全阻塞等待，不会死锁。
            try:
                session = asyncio.run_coroutine_threadsafe(
                    self.api.session_manager.get_session(session_id),
                    event_loop,
                ).result(timeout=10)
            except Exception:
                logger.exception(f"[title_gen] 查询 session 失败，跳过 (session={session_id})")
                return
            if session and (session.get("title") or "").strip():
                logger.debug(
                    f"[title_gen] 跳过：session 已有标题 "
                    f"(session={session_id}, title={session.get('title')!r})"
                )
                return

            try:
                title = self._generate_title(text, config)
            except Exception:
                logger.exception(f"[title_gen] 生成标题失败 (session={session_id})")
                return
            if not title:
                logger.warning(
                    f"[title_gen] LLM 返回空标题，放弃写入 (session={session_id})"
                )
                return
            try:
                asyncio.run_coroutine_threadsafe(
                    self.api.session_manager.update_session(session_id, title=title),
                    event_loop,
                ).result(timeout=10)
            except Exception:
                logger.exception(f"[title_gen] 写入标题失败 (session={session_id})")
                return
            logger.info(f"[title_gen] 标题生成成功 session={session_id} title={title!r}")

        threading.Thread(
            target=worker,
            name=f"title-gen-{session_id}",
            daemon=True,
        ).start()

    def _generate_title(self, user_text: str, config) -> str:
        """一次性 LLM 调用生成标题"""
        # 优先用 title_llm，没配则用主 llm
        llm_cfg = getattr(config, "title_llm", None) or config.llm
        if not (llm_cfg and llm_cfg.model and llm_cfg.api_key):
            logger.warning(
                "[title_gen] LLM 配置不完整，无法生成标题 "
                f"(model={getattr(llm_cfg, 'model', None)!r}, "
                f"api_key={'已配置' if getattr(llm_cfg, 'api_key', None) else '缺失'})"
            )
            return ""

        from ftre_agent_core.llm import LLMHandler, TextDelta

        logger.info(f"[title_gen] 调用 LLM 生成标题 (model={llm_cfg.model})")
        handler = LLMHandler(
            model=llm_cfg.model,
            api_key=llm_cfg.api_key,
            api_base=llm_cfg.api_base,
            api_type=llm_cfg.api_type,
            reasoning_effort=getattr(llm_cfg, "reasoning_effort", ""),
        )
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_text},
        ]

        # LLMHandler.stream 是 async generator，必须在事件循环里消费。
        # worker 跑在独立线程，这里把"消费整个流"作为一个协程投递到主循环执行。
        async def _collect() -> str:
            parts: list[str] = []
            async for item in handler.stream(messages, tools=None):
                if isinstance(item, TextDelta) and item.text:
                    parts.append(item.text)
            return "".join(parts)

        raw = asyncio.run_coroutine_threadsafe(
            _collect(), self.api.event_loop
        ).result(timeout=60).strip()
        logger.info(f"[title_gen] LLM 原始输出={raw!r}")
        return self._sanitize_title(raw)

    def _sanitize_title(self, raw: str) -> str:
        """清洗 LLM 输出：去引号、去前缀、压缩空白、硬截断"""
        if not raw:
            return ""
        s = raw.strip()
        s = s.splitlines()[0].strip()

        # 去外层成对引号
        same_pairs = ('"', "'", "`")
        diff_pairs = (("「", "」"), ("\u201c", "\u201d"), ("\u2018", "\u2019"), ("《", "》"), ("【", "】"))
        for q in same_pairs:
            if len(s) >= 2 and s.startswith(q) and s.endswith(q):
                s = s[1:-1].strip()
                break
        else:
            for left, right in diff_pairs:
                if len(s) >= 2 and s.startswith(left) and s.endswith(right):
                    s = s[1:-1].strip()
                    break

        # 去常见前缀
        for prefix in ("标题：", "标题:", "Title:", "title:"):
            if s.lower().startswith(prefix.lower()):
                s = s[len(prefix):].strip()
                break

        # 末尾标点修剪
        s = s.rstrip("。.!?！？,，;；:：")

        # 硬截断
        if len(s) > self._max_chars:
            s = s[: self._max_chars]
        return s

    @staticmethod
    def _extract_text(inbound_data: dict) -> str:
        """从 inbound_data 提取纯文本"""
        content = inbound_data.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    t = part.get("text", "")
                    if isinstance(t, str) and t:
                        chunks.append(t)
            return "\n".join(chunks).strip()
        return ""
