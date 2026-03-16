"""Fallback model adapter — tries primary, then fallbacks in order.

When the primary adapter's retries are exhausted, this wrapper automatically
tries each fallback adapter before giving up. Auth errors (LLMAuthError) are
never retried on fallbacks because they indicate a credential problem that
alternative models cannot resolve.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from agent_framework.adapters.model.base_adapter import (
    BaseModelAdapter,
    LLMAuthError,
    LLMCallError,
    ModelChunk,
)
from agent_framework.infra.logger import get_logger
from agent_framework.models.message import Message, ModelResponse

logger = get_logger(__name__)


class FallbackModelAdapter(BaseModelAdapter):
    """Wraps a primary adapter with a fallback chain.

    Implements the same interface as BaseModelAdapter. On primary failure
    (after its own retries are exhausted), tries each fallback in order.
    LLMAuthError is NOT retried on fallbacks — it is re-raised immediately.
    """

    def __init__(
        self,
        primary: BaseModelAdapter,
        fallbacks: list[BaseModelAdapter],
    ) -> None:
        super().__init__()
        self._primary = primary
        self._fallbacks = fallbacks

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        adapters = [self._primary, *self._fallbacks]
        last_error: Exception | None = None

        for index, adapter in enumerate(adapters):
            try:
                response = await adapter.complete(
                    messages, tools, temperature, max_tokens,
                )
                if index > 0:
                    logger.info(
                        "fallback.complete.succeeded",
                        adapter_index=index,
                        adapter_type=type(adapter).__name__,
                    )
                return response
            except LLMAuthError:
                # Auth errors cannot be resolved by a different model
                raise
            except LLMCallError as exc:
                last_error = exc
                logger.warning(
                    "fallback.complete.adapter_failed",
                    adapter_index=index,
                    adapter_type=type(adapter).__name__,
                    error=str(exc),
                )
                continue

        # All adapters exhausted — raise the last error
        raise last_error  # type: ignore[misc]

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ModelChunk]:
        adapters = [self._primary, *self._fallbacks]
        last_error: Exception | None = None

        for index, adapter in enumerate(adapters):
            try:
                # stream_complete may be an async generator (yields directly)
                # or a coroutine returning an AsyncIterator. Handle both.
                stream_or_coro = adapter.stream_complete(
                    messages, tools, temperature, max_tokens,
                )
                # If it's a coroutine, await it to get the iterator
                if hasattr(stream_or_coro, "__anext__"):
                    stream = stream_or_coro
                else:
                    stream = await stream_or_coro

                if index > 0:
                    logger.info(
                        "fallback.stream.succeeded",
                        adapter_index=index,
                        adapter_type=type(adapter).__name__,
                    )
                # Yield chunks from stream. Errors raised during iteration
                # (before any chunks are yielded) trigger fallback. Once a
                # chunk has been yielded, mid-stream errors propagate directly
                # because partial data was already sent to the caller.
                started = False
                async for chunk in stream:
                    started = True
                    yield chunk
                return
            except LLMAuthError:
                raise
            except LLMCallError as exc:
                last_error = exc
                logger.warning(
                    "fallback.stream.adapter_failed",
                    adapter_index=index,
                    adapter_type=type(adapter).__name__,
                    error=str(exc),
                )
                continue

        raise last_error  # type: ignore[misc]

    def count_tokens(self, messages: list[Message]) -> int:
        """Delegate to primary adapter for token counting."""
        return self._primary.count_tokens(messages)

    def supports_parallel_tool_calls(self) -> bool:
        """Delegate to primary adapter."""
        return self._primary.supports_parallel_tool_calls()

    def supports_stateful_session(self) -> bool:
        """Delegate to primary adapter."""
        return self._primary.supports_stateful_session()

    def begin_session(self, session_id: str = "") -> None:
        """Propagate session start to all adapters."""
        self._primary.begin_session(session_id)
        for fb in self._fallbacks:
            fb.begin_session(session_id)

    def end_session(self) -> None:
        """Propagate session end to all adapters."""
        self._primary.end_session()
        for fb in self._fallbacks:
            fb.end_session()
