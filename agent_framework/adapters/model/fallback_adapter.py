"""Fallback model adapter — tries primary, then fallbacks in order.

When the primary adapter's retries are exhausted, this wrapper automatically
tries each fallback adapter before giving up. Auth errors (LLMAuthError) are
never retried on fallbacks because they indicate a credential problem that
alternative models cannot resolve.

Integrates with CircuitBreaker to skip adapters in cooldown and classify
failures for exponential backoff.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from agent_framework.adapters.model.auth_profiles import AuthProfile, AuthProfileStore
from agent_framework.adapters.model.base_adapter import (
    BaseModelAdapter,
    LLMAuthError,
    LLMCallError,
    ModelChunk,
)
from agent_framework.adapters.model.circuit_breaker import CircuitBreaker
from agent_framework.adapters.model.failover_types import classify_error
from agent_framework.infra.logger import get_logger
from agent_framework.models.message import Message, ModelResponse

logger = get_logger(__name__)


def _apply_auth_profile(adapter: BaseModelAdapter, profile: AuthProfile) -> None:
    """Apply an auth profile's credentials to an adapter's underlying client.

    Supports adapters with an OpenAI-compatible `_client` attribute (most
    adapters in the framework). Silently skips if the adapter does not
    expose a mutable client.
    """
    client = getattr(adapter, "_client", None)
    if client is None:
        return
    # OpenAI SDK clients expose api_key as a mutable attribute
    if hasattr(client, "api_key"):
        client.api_key = profile.api_key
    if profile.api_base and hasattr(client, "base_url"):
        client.base_url = profile.api_base


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
        circuit_breaker: CircuitBreaker | None = None,
        auth_profile_store: AuthProfileStore | None = None,
    ) -> None:
        super().__init__()
        self._primary = primary
        self._fallbacks = fallbacks
        self._circuit_breaker = circuit_breaker
        self._auth_profile_store = auth_profile_store

    def _adapter_key(self, adapter: BaseModelAdapter) -> str:
        """Derive a stable key for circuit breaker tracking."""
        return f"{type(adapter).__name__}_{id(adapter)}"

    def _profile_scoped_key(self, adapter_key: str, profile_id: str) -> str:
        """Build a circuit breaker key scoped to a specific auth profile."""
        return f"{adapter_key}::profile::{profile_id}"

    def _select_profile(self, adapter_key: str) -> AuthProfile | None:
        """Select the next auth profile using LRU ordering with cooldown awareness."""
        if not self._auth_profile_store:
            return None

        cb = self._circuit_breaker

        def _is_in_cooldown(pid: str) -> bool:
            if not cb:
                return False
            return cb.is_in_cooldown(self._profile_scoped_key(adapter_key, pid))

        def _should_probe(pid: str) -> bool:
            if not cb:
                return False
            return cb.should_probe(self._profile_scoped_key(adapter_key, pid))

        return self._auth_profile_store.select_next(
            is_in_cooldown=_is_in_cooldown,
            should_probe=_should_probe,
        )

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        adapters = [self._primary, *self._fallbacks]
        last_error: Exception | None = None

        if self._circuit_breaker:
            self._circuit_breaker.clear_expired()

        for index, adapter in enumerate(adapters):
            adapter_key = self._adapter_key(adapter)

            # Auth profile rotation: select an available profile for this adapter
            profile = self._select_profile(adapter_key)
            if profile:
                _apply_auth_profile(adapter, profile)
                self._auth_profile_store.mark_used(profile.profile_id)  # type: ignore[union-attr]
                effective_cb_key = self._profile_scoped_key(adapter_key, profile.profile_id)
                logger.info(
                    "fallback.complete.profile_selected",
                    adapter_index=index,
                    profile_id=profile.profile_id,
                )
            else:
                effective_cb_key = adapter_key

            # Skip adapters in cooldown (unless probe is allowed)
            if self._circuit_breaker and self._circuit_breaker.is_in_cooldown(effective_cb_key):
                if self._circuit_breaker.should_probe(effective_cb_key):
                    self._circuit_breaker.consume_probe_slot(effective_cb_key)
                    logger.info(
                        "fallback.complete.probing",
                        adapter_index=index,
                        adapter_type=type(adapter).__name__,
                    )
                else:
                    logger.info(
                        "fallback.complete.skipped_cooldown",
                        adapter_index=index,
                        adapter_type=type(adapter).__name__,
                        cooldown_remaining=self._circuit_breaker.get_cooldown_remaining(effective_cb_key),
                    )
                    continue

            try:
                response = await adapter.complete(
                    messages, tools, temperature, max_tokens,
                )
                if self._circuit_breaker:
                    self._circuit_breaker.record_success(effective_cb_key)
                if profile:
                    self._auth_profile_store.mark_success(profile.profile_id)  # type: ignore[union-attr]
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
                if self._circuit_breaker:
                    reason = classify_error(exc)
                    self._circuit_breaker.record_failure(effective_cb_key, reason)
                if profile:
                    self._auth_profile_store.mark_failure(profile.profile_id)  # type: ignore[union-attr]
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

        if self._circuit_breaker:
            self._circuit_breaker.clear_expired()

        for index, adapter in enumerate(adapters):
            adapter_key = self._adapter_key(adapter)

            # Auth profile rotation: select an available profile for this adapter
            profile = self._select_profile(adapter_key)
            if profile:
                _apply_auth_profile(adapter, profile)
                self._auth_profile_store.mark_used(profile.profile_id)  # type: ignore[union-attr]
                effective_cb_key = self._profile_scoped_key(adapter_key, profile.profile_id)
                logger.info(
                    "fallback.stream.profile_selected",
                    adapter_index=index,
                    profile_id=profile.profile_id,
                )
            else:
                effective_cb_key = adapter_key

            # Skip adapters in cooldown (unless probe is allowed)
            if self._circuit_breaker and self._circuit_breaker.is_in_cooldown(effective_cb_key):
                if self._circuit_breaker.should_probe(effective_cb_key):
                    self._circuit_breaker.consume_probe_slot(effective_cb_key)
                    logger.info(
                        "fallback.stream.probing",
                        adapter_index=index,
                        adapter_type=type(adapter).__name__,
                    )
                else:
                    logger.info(
                        "fallback.stream.skipped_cooldown",
                        adapter_index=index,
                        adapter_type=type(adapter).__name__,
                        cooldown_remaining=self._circuit_breaker.get_cooldown_remaining(effective_cb_key),
                    )
                    continue

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
                if self._circuit_breaker:
                    self._circuit_breaker.record_success(effective_cb_key)
                if profile:
                    self._auth_profile_store.mark_success(profile.profile_id)  # type: ignore[union-attr]
                return
            except LLMAuthError:
                raise
            except LLMCallError as exc:
                last_error = exc
                if self._circuit_breaker:
                    reason = classify_error(exc)
                    self._circuit_breaker.record_failure(effective_cb_key, reason)
                if profile:
                    self._auth_profile_store.mark_failure(profile.profile_id)  # type: ignore[union-attr]
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
        return self._primary.supports_parallel_tool_calls()

    def supports_vision(self) -> bool:
        return self._primary.supports_vision()

    def supports_audio(self) -> bool:
        return self._primary.supports_audio()

    def supports_stateful_session(self) -> bool:
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
