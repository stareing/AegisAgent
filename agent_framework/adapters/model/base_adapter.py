from __future__ import annotations
from abc import ABC, abstractmethod
from typing import AsyncIterator, Any
from agent_framework.models.message import Message, ModelResponse, ToolCallRequest, TokenUsage


class ModelChunk:
    """A chunk from streaming response."""

    def __init__(
        self,
        delta_content: str | None = None,
        delta_tool_calls: list[dict] | None = None,
        finish_reason: str | None = None,
    ):
        self.delta_content = delta_content
        self.delta_tool_calls = delta_tool_calls
        self.finish_reason = finish_reason


class LLMCallError(Exception):
    """Base LLM error."""
    pass


class LLMRateLimitError(LLMCallError):
    """Rate limit hit."""
    pass


class LLMAuthError(LLMCallError):
    """Authentication error."""
    pass


class LLMTimeoutError(LLMCallError):
    """Timeout error."""
    pass


class BaseModelAdapter(ABC):
    """Abstract base for model adapters."""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        ...

    @abstractmethod
    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[ModelChunk]:
        ...

    @abstractmethod
    def count_tokens(self, messages: list[Message]) -> int:
        ...

    def supports_parallel_tool_calls(self) -> bool:
        return False
