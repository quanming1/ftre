"""LLMError：runner 重试策略使用的统一 LLM 错误。

从 completion.py 迁出（B2 LLM 协议适配层，PRD-B2-llm-adapter）。
分类逻辑（classify）保持逐字节等价。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

import openai


@dataclass
class LLMError(Exception):
    """runner 重试策略使用的统一 LLM 错误。"""

    message: str
    code: str

    # 异常类型 → 错误码映射
    _TYPE_MAP: ClassVar[dict] = {
        openai.RateLimitError: "rate_limit",
        openai.APITimeoutError: "timeout",
        openai.APIConnectionError: "network",
        openai.AuthenticationError: "auth_error",
        openai.PermissionDeniedError: "content_filter",
        openai.BadRequestError: "bad_request",
        openai.InternalServerError: "internal_server_error",
        openai.APIError: "api_error",
    }

    # exc.code 优先级覆盖（OpenAI SDK 的 APIError 可能带更精确 code）
    _CODE_OVERRIDES: ClassVar[dict[str, str]] = {
        "invalid_request_error": "bad_request",
        "bad_request": "bad_request",
        "rate_limit_exceeded": "rate_limit",
        "context_length_exceeded": "bad_request",
        "invalid_api_key": "auth_error",
        "authentication_error": "auth_error",
        "permission_denied": "content_filter",
    }

    # 不可重试的错误码
    UNRETRYABLE_CODES: ClassVar[set[str]] = {"auth_error", "bad_request", "content_filter"}

    @staticmethod
    def classify(exc: Exception) -> LLMError:
        # 优先用 SDK 的 exc.code（更精确）
        if isinstance(exc, openai.APIError) and hasattr(exc, "code") and exc.code:
            code = LLMError._CODE_OVERRIDES.get(exc.code)
            if code:
                return LLMError(message=str(exc), code=code)

        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None) if response is not None else None
        if status_code == 400:
            return LLMError(message=str(exc), code="bad_request")
        if status_code in (401, 402):
            return LLMError(message=str(exc), code="auth_error" if status_code == 401 else "bad_request")
        if status_code == 403:
            return LLMError(message=str(exc), code="content_filter")

        message_lower = str(exc).lower()
        if "invalidparameter" in message_lower or "invalid parameter" in message_lower:
            return LLMError(message=str(exc), code="bad_request")

        # 按异常类型映射
        for exc_type, code in LLMError._TYPE_MAP.items():
            if isinstance(exc, exc_type):
                return LLMError(message=str(exc), code=code)

        # httpx 兜底
        try:
            import httpx
            if isinstance(exc, httpx.RemoteProtocolError):
                return LLMError(message=str(exc), code="network")
            if isinstance(exc, httpx.ReadTimeout):
                return LLMError(message=str(exc), code="timeout")
        except ImportError:
            pass

        return LLMError(message=str(exc), code="unknown")


def get_attr(obj: Any, name: str, default: Any = None) -> Any:
    """dict / object 双形态取属性（从 completion.py 迁出的内部辅助）。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)



class LlmServiceError(RuntimeError):
    """LLM Service 在请求尚未发出前发现的稳定错误。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AdapterNotFoundError(LlmServiceError):
    """请求的 api_type 没有注册 Adapter。"""

    def __init__(self, api_type: str) -> None:
        super().__init__(f"no LLM adapter registered for api_type={api_type!r}", "NO_ADAPTER")


class PreparedCallError(LlmServiceError):
    """PreparedCall 被重复使用或与原始配置不匹配。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, "INVALID_PREPARED_CALL")

