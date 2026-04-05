"""Anthropic SDK adapter for the agent framework.

Uses the official `anthropic` package directly. Install with:
    pip install agent-framework[anthropic]

Key differences from OpenAI format:
- System message is a separate parameter, not in the messages list
- Tool calls use content blocks (type=tool_use), not top-level tool_calls
- Tool results use role=user with content blocks (type=tool_result)
- Tool schemas use top-level name/description/input_schema (not wrapped in function:{})
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, AsyncIterator

from agent_framework.adapters.model.base_adapter import (
    BaseModelAdapter,
    LLMAuthError,
    LLMCallError,
    LLMRateLimitError,
    LLMTimeoutError,
    ModelChunk,
)
from agent_framework.models.message import (
    Message,
    ModelResponse,
    ToolCallRequest,
    TokenUsage,
)

logger = logging.getLogger(__name__)


class AnthropicAdapter(BaseModelAdapter):
    """Model adapter backed by the official Anthropic SDK."""

    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        api_base: str | None = None,
        timeout_ms: int = 60_000,
        max_retries: int = 3,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
    ) -> None:
        super().__init__()
        import anthropic

        self.model_name = model_name
        self.max_retries = max_retries
        self.default_temperature = temperature
        self.max_output_tokens = max_output_tokens

        client_kwargs: dict[str, Any] = {
            "timeout": timeout_ms / 1000.0,
            "max_retries": 0,
        }
        if api_key:
            client_kwargs["api_key"] = api_key
        if api_base:
            client_kwargs["base_url"] = api_base

        self._client = anthropic.AsyncAnthropic(**client_kwargs)
        self._anthropic = anthropic

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        kwargs = self._build_kwargs(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens,
        )
        raw = await self._call_with_retry(kwargs)
        return self._parse_response(raw)

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ModelChunk]:
        kwargs = self._build_kwargs(
            messages, tools=tools, temperature=temperature,
            max_tokens=max_tokens, stream=True,
        )
        kwargs.pop("stream", None)
        async with self._client.messages.stream(**kwargs) as stream:
            async for event in stream:
                chunk = self._parse_stream_event(event)
                if chunk is not None:
                    yield chunk

    def count_tokens(self, messages: list[Message]) -> int:
        # Rough estimate — Anthropic SDK's count_tokens is sync and
        # requires the full message format conversion. Use char-based estimate.
        text = "".join((m.content or "") for m in messages)
        return len(text) // 4

    def supports_parallel_tool_calls(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        return True  # All Claude 3+ models support vision

    # ------------------------------------------------------------------
    # Message & tool conversion (Anthropic format)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_system(messages: list[Message]) -> tuple[Any, list[Message]]:
        """Separate system messages from the conversation.

        Anthropic requires the system prompt as a separate parameter.
        When any system message carries cache_control, returns a list of
        content blocks (Anthropic's structured system format) instead of
        a plain string, enabling prompt caching.

        Returns:
            (system, non_system) where system is str | list[dict] | None.
        """
        system_parts: list[str] = []
        has_cache_control = False
        non_system: list[Message] = []
        for m in messages:
            if m.role == "system":
                if m.content:
                    system_parts.append(m.content)
                if m.cache_control:
                    has_cache_control = True
            else:
                non_system.append(m)

        if not system_parts:
            return None, non_system

        # v4.3: When cache_control is present, use structured system format
        # so Anthropic caches the system prompt prefix
        if has_cache_control:
            system_text = "\n\n".join(system_parts)
            return [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}], non_system

        return "\n\n".join(system_parts), non_system

    @staticmethod
    def _convert_messages(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert framework Messages to Anthropic message format.

        Key conversions:
        - assistant messages with tool_calls → content blocks with tool_use type
        - tool messages → user messages with tool_result content blocks
        - Consecutive same-role messages are merged (Anthropic requirement)
        - v4.3: cache_control propagated to last content block of marked messages
        """
        result: list[dict[str, Any]] = []
        # v4.3: Track which result indices need cache_control applied
        # (needed because user messages get merged)
        cache_control_indices: dict[int, dict] = {}

        for m in messages:
            if m.role == "assistant":
                content_blocks: list[dict[str, Any]] = []
                if m.content:
                    content_blocks.append({"type": "text", "text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.function_name,
                            "input": tc.arguments,
                        })
                if not content_blocks:
                    content_blocks.append({"type": "text", "text": ""})
                # v4.3: Inject cache_control on last content block
                if m.cache_control and content_blocks:
                    content_blocks[-1]["cache_control"] = m.cache_control
                result.append({"role": "assistant", "content": content_blocks})

            elif m.role == "tool":
                # Anthropic: tool results are user messages with tool_result blocks
                tool_result_block = {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": m.content or "",
                }
                # Merge into previous user message if consecutive
                if result and result[-1]["role"] == "user" and isinstance(result[-1]["content"], list):
                    result[-1]["content"].append(tool_result_block)
                else:
                    result.append({"role": "user", "content": [tool_result_block]})
                if m.cache_control:
                    cache_control_indices[len(result) - 1] = m.cache_control

            elif m.role == "user":
                if m.content_parts:
                    blocks = AnthropicAdapter._convert_content_parts(m.content_parts)
                    # Merge into previous user message if consecutive
                    if result and result[-1]["role"] == "user" and isinstance(result[-1].get("content"), list):
                        result[-1]["content"].extend(blocks)
                    else:
                        result.append({"role": "user", "content": blocks})
                else:
                    # Merge consecutive user messages (text-only)
                    if result and result[-1]["role"] == "user" and isinstance(result[-1].get("content"), str):
                        result[-1]["content"] += "\n" + (m.content or "")
                    elif result and result[-1]["role"] == "user" and isinstance(result[-1].get("content"), list):
                        result[-1]["content"].append({"type": "text", "text": m.content or ""})
                    else:
                        result.append({"role": "user", "content": m.content or ""})
                if m.cache_control:
                    cache_control_indices[len(result) - 1] = m.cache_control

        # v4.3: Apply cache_control to last content block of marked messages
        for idx, cc in cache_control_indices.items():
            msg_content = result[idx].get("content")
            if isinstance(msg_content, list) and msg_content:
                msg_content[-1]["cache_control"] = cc
            elif isinstance(msg_content, str):
                result[idx]["content"] = [{"type": "text", "text": msg_content, "cache_control": cc}]

        return result

    @staticmethod
    def _convert_content_parts(parts: list) -> list[dict[str, Any]]:
        """Convert framework ContentParts to Anthropic content blocks."""
        result: list[dict[str, Any]] = []
        for p in parts:
            if p.type == "text":
                result.append({"type": "text", "text": p.text or ""})
            elif p.type == "image_url":
                result.append({
                    "type": "image",
                    "source": {"type": "url", "url": p.image_url or ""},
                })
            elif p.type == "image_base64":
                result.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": p.media_type or "image/png",
                        "data": p.data or "",
                    },
                })
            elif p.type == "audio":
                # Anthropic doesn't natively support audio — include as text reference
                result.append({"type": "text", "text": f"[Audio: {p.media_type or 'audio'}]"})
            elif p.type == "file":
                result.append({"type": "text", "text": f"[File: {p.file_uri or ''}]"})
        return result

    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict[str, Any]]:
        """Convert OpenAI-format tool schemas to Anthropic format.

        OpenAI:    {"type": "function", "function": {"name", "description", "parameters"}}
        Anthropic: {"name", "description", "input_schema"}
        """
        converted: list[dict[str, Any]] = []
        for t in tools:
            func = t.get("function", t)
            converted.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return converted

    # ------------------------------------------------------------------
    # Build & retry
    # ------------------------------------------------------------------

    def _build_kwargs(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        system, non_system = self._extract_system(messages)
        formatted = self._convert_messages(non_system)

        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": formatted,
            "temperature": temperature if temperature is not None else self.default_temperature,
            "max_tokens": max_tokens or self.max_output_tokens,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        if stream:
            kwargs["stream"] = True
        return kwargs

    async def _call_with_retry(self, kwargs: dict[str, Any]) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return await self._client.messages.create(**kwargs)
            except self._anthropic.RateLimitError as exc:
                last_exc = LLMRateLimitError(str(exc))
                logger.warning("Rate limit hit (attempt %d/%d)", attempt + 1, self.max_retries)
            except self._anthropic.AuthenticationError as exc:
                raise LLMAuthError(str(exc)) from exc
            except self._anthropic.APITimeoutError as exc:
                last_exc = LLMTimeoutError(str(exc))
                logger.warning("Timeout (attempt %d/%d)", attempt + 1, self.max_retries)
            except self._anthropic.APIError as exc:
                last_exc = LLMCallError(str(exc))
                logger.warning("API error (attempt %d/%d): %s", attempt + 1, self.max_retries, exc)

            if attempt < self.max_retries - 1:
                backoff = min(2 ** attempt + random.random(), 30.0)
                await asyncio.sleep(backoff)

        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, raw: Any) -> ModelResponse:
        """Convert Anthropic response to ModelResponse.

        Anthropic response.content is a list of content blocks:
        - {"type": "text", "text": "..."}
        - {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
        """
        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []

        for block in raw.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCallRequest(
                        id=block.id,
                        function_name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )

        content = "\n".join(text_parts) if text_parts else None

        # Map stop_reason
        stop_reason = getattr(raw, "stop_reason", "end_turn")
        if stop_reason == "tool_use" or tool_calls:
            finish = "tool_calls"
        elif stop_reason == "max_tokens":
            finish = "length"
        else:
            finish = "stop"

        # Token usage
        usage = getattr(raw, "usage", None)
        token_usage = TokenUsage()
        if usage is not None:
            input_tokens = getattr(usage, "input_tokens", 0) or 0
            output_tokens = getattr(usage, "output_tokens", 0) or 0
            token_usage = TokenUsage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            )

        raw_meta: dict[str, Any] = {
            "model": getattr(raw, "model", self.model_name),
            "response_id": getattr(raw, "id", ""),
        }

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=token_usage,
            raw_response_meta=raw_meta,
        )

    @staticmethod
    def _parse_stream_event(event: Any) -> ModelChunk | None:
        """Convert an Anthropic streaming event to a ModelChunk."""
        event_type = getattr(event, "type", "")

        if event_type == "content_block_delta":
            delta = event.delta
            if getattr(delta, "type", "") == "text_delta":
                return ModelChunk(delta_content=delta.text)
            if getattr(delta, "type", "") == "input_json_delta":
                # Partial tool input JSON — emit as tool call delta
                return ModelChunk(delta_tool_calls=[{
                    "index": getattr(event, "index", 0),
                    "function": {"arguments": delta.partial_json},
                }])

        if event_type == "content_block_start":
            block = event.content_block
            if getattr(block, "type", "") == "tool_use":
                return ModelChunk(delta_tool_calls=[{
                    "index": getattr(event, "index", 0),
                    "id": block.id,
                    "function": {"name": block.name, "arguments": ""},
                }])

        if event_type == "message_delta":
            stop_reason = getattr(event.delta, "stop_reason", None)
            if stop_reason:
                finish = "tool_calls" if stop_reason == "tool_use" else "stop"
                return ModelChunk(finish_reason=finish)

        return None
