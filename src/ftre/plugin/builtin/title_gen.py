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

    def _on_build(self, ctx):
        """before_messages_build hook：首条消息时异步生成标题"""
        # 判断是否首条：events 为空 = DB 里还没有任何事件
        if ctx.events:
            return ctx

        session_id = ctx.session_id
        inbound_data = ctx.inbound_data
        config = ctx.config
        event_loop = self.api.event_loop

        # 检查 session 是否已有 title
        try:
            session = asyncio.run_coroutine_threadsafe(
                self.api.session_manager.get_session(session_id),
                event_loop,
            ).result(timeout=5)
            if session and (session.get("title") or "").strip():
                return ctx
        except Exception:
            return ctx

        # 提取文本
        text = self._extract_text(inbound_data)
        if not text:
            return ctx
        text = text[: self._input_truncate]

        # 异步生成标题（不阻塞主流程）
        self._spawn_title_generation(session_id, text, config, event_loop)
        return ctx

    def _spawn_title_generation(
        self, session_id: str, text: str, config, event_loop
    ) -> None:
        """起守护线程跑 LLM 生成标题"""

        def worker() -> None:
            try:
                title = self._generate_title(text, config)
            except Exception:
                logger.exception(f"[title_gen] 生成标题失败 (session={session_id})")
                return
            if not title:
                return
            try:
                asyncio.run_coroutine_threadsafe(
                    self.api.session_manager.update_session(session_id, title=title),
                    event_loop,
                ).result(timeout=10)
            except Exception:
                logger.exception(f"[title_gen] 写入标题失败 (session={session_id})")
                return
            logger.info(f"[title_gen] session={session_id} title={title!r}")

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
            return ""

        from ftre_agent_core.llm import LLMHandler, LLMResponse, StreamDelta

        handler = LLMHandler(
            model=llm_cfg.model,
            api_key=llm_cfg.api_key,
            api_base=llm_cfg.api_base,
            api_type=llm_cfg.api_type,
        )
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_text},
        ]
        chunks: list[str] = []
        for item in handler.stream(messages, tools=None):
            if isinstance(item, StreamDelta) and item.content:
                chunks.append(item.content)
            elif isinstance(item, LLMResponse) and item.content:
                chunks.append(item.content)
        raw = "".join(chunks).strip()
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
