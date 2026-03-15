from __future__ import annotations
from abc import ABC, abstractmethod
from typing import AsyncIterator, Any
from agent_framework.models.message import Message, ModelResponse, ToolCallRequest, TokenUsage


class ModelChunk:
    """A chunk from streaming response.

    Streaming boundary (v2.6.1 §34):
    - ModelChunk is for streaming output ONLY (consumed by integration layer).
    - ModelChunk MUST NOT be written directly into SessionState.
    - Only the final merged ModelResponse (after stream completes) enters
      the runtime pipeline: AgentLoop → IterationResult → MessageProjector → SessionState.
    - If streaming is interrupted before completion, NO assistant message
      is written to SessionState — only a structured failure audit record.
    - Integration layer may consume chunks for UI rendering but must not
      treat them as authoritative session history.
    """

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


class SessionMode:
    """Tracks stateful session for KV cache optimization.

    Two modes:
    - STATELESS (default): Every request sends full messages list.
      Works with all providers. Provider may still do prefix caching.
    - STATEFUL: First request sends full messages. Subsequent requests
      send only the new messages (delta). Requires provider support.

    Lifecycle:
    1. begin_session() — marks session start, clears cache
    2. complete() with session_mode — adapter decides full vs delta
    3. end_session() — cleanup

    Adapters that support stateful mode override supports_stateful_session().
    """

    def __init__(self) -> None:
        self.active: bool = False
        self.session_id: str = ""
        self.sent_message_count: int = 0
        self.prefix_hash: str = ""

    def reset(self) -> None:
        self.active = False
        self.session_id = ""
        self.sent_message_count = 0
        self.prefix_hash = ""


class BaseModelAdapter(ABC):
    """Abstract base for model adapters.

    Session-aware completion (KV cache optimization):
    - Adapters that support stateful sessions override
      supports_stateful_session() → True and handle delta messages
      in complete(). The framework calls begin_session/end_session
      around a run.
    - Default: stateless (full messages every call).
    """

    def __init__(self, **kwargs: Any) -> None:
        self._session = SessionMode()
        self._session_mode_config: str = "stateless"  # set by entry.py from config

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
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ModelChunk]:
        ...

    @abstractmethod
    def count_tokens(self, messages: list[Message]) -> int:
        ...

    def supports_parallel_tool_calls(self) -> bool:
        return False

    def supports_stateful_session(self) -> bool:
        """Returns True if stateful mode is enabled via config.

        Controlled by config.model.session_mode = "stateful".
        Adapters can override to force-disable for incompatible providers.
        """
        return self._session_mode_config == "stateful"

    def begin_session(self, session_id: str = "") -> None:
        """Start a stateful session. Called by RunCoordinator at run start."""
        self._session.active = True
        self._session.session_id = session_id
        self._session.sent_message_count = 0

    def end_session(self) -> None:
        """End the stateful session. Called by RunCoordinator in finally."""
        self._session.reset()

    def get_delta_messages(self, full_messages: list[Message]) -> list[Message]:
        """Extract only new messages since last send.

        For stateful adapters: returns messages[sent_count:].
        For stateless adapters: returns full list (no optimization).
        """
        if not self._session.active or not self.supports_stateful_session():
            return full_messages

        if self._session.sent_message_count == 0:
            # First call in session — send everything
            self._session.sent_message_count = len(full_messages)
            return full_messages

        # Subsequent calls — only new messages
        delta = full_messages[self._session.sent_message_count:]
        self._session.sent_message_count = len(full_messages)
        return delta if delta else full_messages  # fallback if no delta
