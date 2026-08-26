"""OpenAI 适配器共享骨架（PRD-B2 FR1）。

唯一契约位于 ``contracts.LlmAdapter``；本模块只提供 OpenAI 协议适配器共用
的实现骨架，不再声明第二个 Adapter 抽象。

OpenAIAdapterBase 提供 openai SDK 系适配器的共享骨架：
- AsyncOpenAI 客户端构造（参数与原 LLMHandler.__init__ 一致）
- cancel 机制（flag + 下一个 chunk 检查点退出）
- LLM 日志生命周期（log_input / log_chunk / flush）
- 异常统一包裹：provider 异常转终止性 error finish chunk
  （取消转 aborted finish）——消费方永不面对裸异常

注意：base 层不做 chunk 产出（那是具体适配器 stream() 的职责），
但提供 _wrap_stream() 帮助子类实现"异常 → 终止 finish"的边界收敛。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncGenerator
from typing import Any

import openai

from .contracts import LlmAdapter
from .errors import LLMError
from .events import FinishChunk, FinishReason, LlmFailure, StreamChunk
from .utils import LLMLogger

logger = logging.getLogger(__name__)


class OpenAIAdapterBase(LlmAdapter):
    """openai SDK 系适配器的共享骨架。

    构造参数与现有 OpenAI-compatible 配置一致；协议选择由上层 LlmService
    Registry 完成，适配器自身不再分支。
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        api_base: str | None = None,
        api_type: str = "completions",
        timeout: float = 120.0,
        max_retries: int = 3,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str = "",
        stop: tuple[str, ...] = (),
    ):
        self.model = model
        self.api_type = api_type
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort or ""
        self.stop = tuple(stop)
        self._active_stream = None
        self._active_loop: asyncio.AbstractEventLoop | None = None
        self._cancelled: bool = False
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=timeout,
            max_retries=max_retries,
        )

    def cancel(self) -> None:
        """取消当前请求：设置 flag 让 stream 循环主动退出。

        在主循环化之后，cancel() 在主循环的同步上下文里被调用，
        而 stream() 也在同一个循环里 await。所以不需要
        call_soon_threadsafe 跨循环投递——只需设置 _cancelled flag，
        stream 循环会在下一个 chunk 到达时检查 flag 并主动 break。
        """
        if self._active_stream is None:
            return
        self._cancelled = True
        self._active_stream = None

    async def resolve_model_info(self, model: str):
        """OpenAI-compatible Adapter 不维护远程模型目录，返回未知能力。"""

        return

    def retry_policy(self):
        """Retry 由独立 Plugin 决策，Adapter 不偷偷重试。"""

        return

    # ── 共享的流包装：异常 → 终止 finish ─────────────────────────────

    async def _emit_with_failure_boundary(
        self,
        chunk_gen: AsyncGenerator[StreamChunk, None],
        response: Any,
        llm_log: LLMLogger,
    ) -> AsyncGenerator[StreamChunk, None]:
        """驱动子类的 chunk 产出，把异常收敛为终止性 finish chunk。

        - 子类在生成 chunk 过程中抛出的任何异常 → LLMError.classify
          → finish {kind: "error", failure}（其后无内容）
        - 取消（self._cancelled 置位导致子类 break）→ finish {kind: "aborted"}
        - 正常结束但子类没有产出 finish（协议违规兜底）→
          finish {kind: "error", STREAM_CLOSED}
        - finally：关闭底层 response 与日志

        子类 stream() 应把真正的 chunk 循环实现为内部生成器并交给本方法，
        保证所有路径都以 finish 收尾。
        """
        emitted_finish = False
        try:
            async for chunk in chunk_gen:
                if isinstance(chunk, FinishChunk):
                    emitted_finish = True
                yield chunk
        except Exception as exc:  # noqa: BLE001 - normalize provider failures
            err = LLMError.classify(exc)
            logger.warning("[adapter] stream failed: %s (%s)", err.message[:200], err.code)
            yield _error_finish(err)
            emitted_finish = True
        finally:
            self._active_stream = None
            self._active_loop = None
            was_cancelled = self._cancelled
            self._cancelled = False
            if response is not None:
                close_result = response.close()
                if inspect.isawaitable(close_result):
                    try:
                        await close_result
                    except Exception:
                        logger.debug("failed to close provider response", exc_info=True)
            llm_log.flush()
        if not emitted_finish:
            # 取消：子类 break 后未发 finish；或协议违规（未以 finish 收尾）
            if was_cancelled:
                yield FinishChunk(reason=FinishReason(
                    kind="aborted",
                    failure=LlmFailure(message="stream cancelled by cancel()", code="ABORTED"),
                ))
            else:
                yield _error_finish(LLMError("stream ended without finish chunk", "STREAM_CLOSED"))


def _coerce_request(request, tools=None) -> tuple[list[dict], list[dict] | None]:
    """把标准 LlmRequest 转成协议 Adapter 使用的 wire 字典。"""

    if hasattr(request, "wire_messages"):
        messages = request.wire_messages()
        request_tools = getattr(request, "tools", ())
        tools = [tool.to_mapping() if hasattr(tool, "to_mapping") else dict(tool) for tool in request_tools]
        return messages, tools or None
    return [dict(message) for message in request], tools


def _error_finish(err: LLMError) -> FinishChunk:
    return FinishChunk(reason=FinishReason(
        kind="error",
        failure=LlmFailure(message=err.message, code=err.code),
    ))
