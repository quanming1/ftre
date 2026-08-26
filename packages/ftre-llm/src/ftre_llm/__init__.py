"""ftre 统一 LLM Service 公共入口。"""

from .base import OpenAIAdapterBase
from .block_assembler import BlockAssembler
from .contracts import (
    AdaptersUpdatedPayload,
    AgentRequestPayload,
    LlmAdapter,
    LlmCallConfig,
    LlmCredentials,
    LlmRequest,
    LlmStreamPayload,
    ModelInfo,
    PreparedLlmCall,
    ProviderInfo,
)
from .errors import AdapterNotFoundError, LLMError, LlmServiceError, PreparedCallError
from .events import (
    BlockEnd,
    BlockStart,
    FinishChunk,
    FinishReason,
    LlmFailure,
    ReasoningDeltaChunk,
    StreamChunk,
    TextDeltaChunk,
    ToolCall,
    ToolCallDeltaChunk,
    UsageChunk,
)
from .service import LlmService
from .service_adapter import LlmServiceAdapter

__all__ = [
    "AdapterNotFoundError",
    "AdaptersUpdatedPayload",
    "AgentRequestPayload",
    "BlockAssembler",
    "BlockEnd",
    "BlockStart",
    "FinishChunk",
    "FinishReason",
    "LLMError",
    "LlmAdapter",
    "LlmCallConfig",
    "LlmCredentials",
    "LlmFailure",
    "LlmRequest",
    "LlmService",
    "LlmServiceAdapter",
    "LlmServiceError",
    "LlmStreamPayload",
    "ModelInfo",
    "OpenAIAdapterBase",
    "PreparedCallError",
    "PreparedLlmCall",
    "ProviderInfo",
    "ReasoningDeltaChunk",
    "StreamChunk",
    "TextDeltaChunk",
    "ToolCall",
    "ToolCallDeltaChunk",
    "UsageChunk",
]
