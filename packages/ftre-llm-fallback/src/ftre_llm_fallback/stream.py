"""安全地把“一次主模型流”包装成“必要时切一次备用模型”的流。

这里最重要的不变量是：只要主模型已经向 Core 提交过正文、思考、Tool Call 或块边界，
就绝不能无感切换模型。否则两个模型的输出会被拼成同一条 Assistant 消息，甚至产生重复
Tool Call。Fallback 因此只发生在最后一次 attempt、主模型零输出失败、错误白名单命中时。

本模块没有 Retry Loop。备用模型只调用一次，且 Adapter 的 ``max_retries`` 固定为 0；
备用模型也失败时，把最初的主模型错误交还 Core，保留最有意义的失败原因。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from ftre_llm import (
    BlockEnd,
    BlockStart,
    FinishChunk,
    FinishReason,
    LlmCallConfig,
    LlmCredentials,
    LLMError,
    LlmFailure,
    LlmRequest,
    LlmStreamPayload,
    ReasoningDeltaChunk,
    TextDeltaChunk,
    ToolCallDeltaChunk,
    UsageChunk,
)

from .config import FallbackConfig

logger = logging.getLogger(__name__)

# Provider 往往把上下文超长粗略归类为 bad_request，所以除了错误码，还要检查错误文本。
# 这些错误必须交给 Compaction Plugin，不能被备用模型切换“吃掉”。
_OVERFLOW_MARKERS = ("overflow", "context_length", "context length", "too_long", "too long")


async def stream_with_fallback(
    payload: LlmStreamPayload,
    primary: AsyncIterator[Any],
    config_service: Any,
    config: FallbackConfig,
    llm_service,
) -> AsyncIterator[Any]:
    """先消费主模型流；满足严格条件时改为产出一次备用模型流。

    函数本身是异步生成器。调用它不会立即访问网络，直到 Core 对返回值执行
    ``async for`` 才开始消费 ``primary``。这保证流的背压、取消和异常仍由 Core 驱动。
    """

    # 暂存主错误而不立刻 yield：一旦 yield 给 Core，Core 会把它转换成 LLMError，
    # 当前异步流随即结束，Plugin 就再也没有机会无缝提供备用流。
    primary_error: FinishChunk | None = None

    # committed 表示主模型是否已经改变了对外协议状态。UsageChunk 不算提交；正文、
    # 推理、Tool Call 和 Block 边界都算。提交后切模型会污染消息和工具调用配对。
    committed = False
    try:
        async for chunk in primary:
            if isinstance(chunk, FinishChunk):
                kind = chunk.reason.kind
                if kind == "aborted":
                    # aborted 通常来自用户取消。取消是明确控制信号，不能偷偷换模型继续跑。
                    yield chunk
                    return
                if kind == "error":
                    primary_error = chunk
                    code, message = _failure_details(chunk)
                    if committed or not _can_fallback(payload, config, code, message):
                        # 不满足接管条件时保持原协议，让 Core 正常决定 Retry/Stop。
                        yield chunk
                        return
                    # 先不把主模型的 error finish 交给 Core，等备用流结果。
                    break

                # stop/tool_calls 等正常结束必须原样转发；正常完成没有 fallback 的理由。
                yield chunk
                return
            if _commits_output(chunk):
                committed = True
            yield chunk
    except asyncio.CancelledError:
        # asyncio 的任务取消必须保持向上传播；它与 Provider 返回 aborted 具有同样优先级。
        raise
    except Exception as exc:
        # 适配器既可能返回 FinishChunk(error)，也可能直接抛异常。两条路径统一成同一套
        # 白名单判断，但已提交输出后的异常必须原样抛回 Core。
        if committed:
            raise
        error = exc if isinstance(exc, LLMError) else LLMError.classify(exc)
        if not _can_fallback(payload, config, error.code, error.message):
            raise
        primary_error = _error_finish(error)

    # 主模型已经给出终止错误时显式关闭异步生成器，确保 Adapter 的 response/log/连接
    # finally 立即执行，不把连接释放交给垃圾回收，也不让两条模型流同时占用资源。
    await _close_stream(primary)

    if primary_error is None:
        # 主流自然耗尽却没有 FinishChunk：这里不凭空制造成功或错误事件。
        return

    try:
        # Package 只保存逻辑名称；凭据和 api_type 从 Host 的单一 ConfigService 获取。
        # resolve_llm 返回防御性快照，Package 不读取 config.json 或 AgentProfile 私有状态。
        resolved = config_service.resolve_llm(config.provider, config.model)
        if not isinstance(resolved, Mapping):
            yield primary_error
            return
        prepared = await _prepare_backup_call(llm_service, payload, resolved)
    except Exception as exc:  # noqa: BLE001 - optional fallback must not break Core
        logger.warning(
            "[llm-fallback] backup adapter unavailable provider=%s model=%s error=%s",
            config.provider,
            config.model,
            type(exc).__name__,
        )
        yield primary_error
        return

    # 备用失败期间不把它的 partial/error finish 交给 Core。最终统一回传主模型错误，
    # 防止一次 attempt 同时暴露两个 Provider 的失败协议。
    backup_failed = False
    try:
        # 防御性复制消息和工具定义，避免 Adapter 对嵌套顶层字典的修改污染 Hook Payload。
        request = LlmRequest.from_parts(
            prepared.config,
            payload.messages,
            payload.tools,
            purpose=payload.purpose,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            turn_id=payload.turn_id,
            cancellation=payload.cancellation,
            attempt=payload.attempt,
            max_attempts=payload.max_attempts,
        )
        async for chunk in prepared.stream(request):
            if isinstance(chunk, FinishChunk) and chunk.reason.kind in {"error", "aborted"}:
                backup_failed = True
                continue
            yield chunk
            if isinstance(chunk, FinishChunk):
                return
        backup_failed = True
    except asyncio.CancelledError:
        # Core 取消备用请求时，同时通知 Adapter 中止底层 HTTP 流，再把取消继续向上抛。
        prepared.cancel()
        raise
    except Exception:  # noqa: BLE001 - backup failure returns the primary error
        backup_failed = True
    if backup_failed:
        # 备用调用失败不递归 fallback；Core 看到的是最初主模型错误。
        yield primary_error


def _can_fallback(
    payload: LlmStreamPayload,
    config: FallbackConfig,
    code: str,
    message: str,
) -> bool:
    """判断当前主错误是否允许被备用模型接管。

    条件采用全量 AND：必须是最后一次 attempt、未取消、Plugin 配置完整、错误未被
    exclude/overflow 排除并且命中白名单。任意条件不满足都保留 Core 原行为。
    """

    # 前 N-1 次 attempt 必须先让 Core Retry；否则会变成“每次失败都切备用模型”。
    if payload.attempt != payload.max_attempts or payload.cancellation.is_set() or not config.enabled:
        return False
    value = f"{code} {message}".strip().lower()
    if not code or code in config.exclude_errors or any(marker in value for marker in _OVERFLOW_MARKERS):
        return False
    return code in config.errors


def _commits_output(chunk: Any) -> bool:
    """判断 chunk 是否已经让 Core 的消息/块协议进入不可回滚状态。

    Usage 只是计量信息，不影响消息结构；空文本/空参数 delta 也不算。BlockStart 即使没有
    可见文本也必须算已提交，因为切模型后新的 BlockEnd 无法与旧模型的 BlockStart 配对。
    """

    if isinstance(chunk, UsageChunk):
        return False
    if isinstance(chunk, TextDeltaChunk | ReasoningDeltaChunk):
        return bool(chunk.text)
    if isinstance(chunk, ToolCallDeltaChunk):
        return bool(chunk.call_id or chunk.name or chunk.arguments_delta)
    return isinstance(chunk, BlockStart | BlockEnd)


def _failure_details(chunk: FinishChunk) -> tuple[str, str]:
    """从 error FinishChunk 中安全取出归一化错误码和原始错误文本。"""

    failure = chunk.reason.failure
    return (
        failure.code.lower() if failure and isinstance(failure.code, str) else "",
        failure.message if failure else "",
    )


def _error_finish(error: LLMError) -> FinishChunk:
    """把直接抛出的 LLMError 转成与 Adapter 错误流一致的 FinishChunk。"""

    return FinishChunk(
        reason=FinishReason(
            kind="error",
            failure=LlmFailure(message=error.message, code=error.code),
        )
    )


async def _prepare_backup_call(llm_service, payload: LlmStreamPayload, resolved: Mapping[str, Any]):
    """通过唯一 LlmService 准备一次备用调用，避免递归进入 fallback Hook。"""

    config = LlmCallConfig(
        provider=str(resolved.get("provider") or "backup"),
        model=str(resolved["model"]),
        api_type=str(resolved.get("api_type") or "completions"),
        max_tokens=resolved.get("max_output") if isinstance(resolved.get("max_output"), int) else None,
        reasoning_effort=str(resolved.get("reasoning_effort") or "") or None,
    )
    credentials = LlmCredentials(
        api_key=str(resolved.get("api_key") or ""),
        api_base=str(resolved.get("api_base") or ""),
    )
    return await llm_service.prepare_call(config, credentials=credentials)


async def _close_stream(stream: AsyncIterator[Any]) -> None:
    """尽力关闭主异步流；清理失败只记 debug，不覆盖真正的 LLM 错误。"""

    # AsyncIterator 协议不强制提供 aclose，因此先做能力探测。
    close = getattr(stream, "aclose", None)
    if close is not None:
        try:
            await close()
        except Exception:
            logger.debug("[llm-fallback] primary stream close failed", exc_info=True)


__all__ = ["stream_with_fallback"]
