"""OpenAI SDK adapter for the agent framework.

Uses the official `openai` package directly. Install with:
    pip install agent-framework[openai]
"""

from __future__ import annotations

import asyncio
import json
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
    ContentPart,
    Message,
    ModelResponse,
    ToolCallRequest,
    TokenUsage,
)

logger = logging.getLogger(__name__)


class OpenAIAdapter(BaseModelAdapter):
    """Model adapter backed by the official OpenAI SDK.

    Message and tool schema formats are natively compatible with OpenAI's API,
    so conversion is minimal (nearly identical to LiteLLMAdapter).
    """

    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        api_base: str | None = None,
        timeout_ms: int = 60_000,
        max_retries: int = 3,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> None:
        super().__init__()
        import openai

        self.model_name = model_name
        self.max_retries = max_retries
        self.default_temperature = temperature
        self.max_output_tokens = max_output_tokens

        client_kwargs: dict[str, Any] = {
            "timeout": timeout_ms / 1000.0,
            "max_retries": 0,  # we handle retries ourselves
        }
        if api_key:
            client_kwargs["api_key"] = api_key
        if api_base:
            client_kwargs["base_url"] = api_base

        self._client = openai.AsyncOpenAI(**client_kwargs)
        self._openai = openai  # keep module ref for exception types

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
        raw_stream = await self._call_with_retry(kwargs)
        async for chunk in raw_stream:
            yield self._parse_chunk(chunk)

    def count_tokens(self, messages: list[Message]) -> int:
        try:
            import tiktoken

            enc = tiktoken.encoding_for_model(self.model_name)
            total = 0
            for m in messages:
                total += 4  # message overhead
                if m.content:
                    total += len(enc.encode(m.content))
                if m.tool_calls:
                    for tc in m.tool_calls:
                        total += len(enc.encode(json.dumps(tc.arguments)))
                        total += len(enc.encode(tc.function_name))
            return total
        except Exception:
            text = "".join(
                (m.content or "") for m in messages
            )
            return len(text) // 4

    def supports_parallel_tool_calls(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        # gpt-4o, gpt-4-turbo, gpt-4o-mini all support vision
        return True

    def supports_audio(self) -> bool:
        return "audio" in self.model_name.lower()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _messages_to_dicts(messages: list[Message]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for m in messages:
            d: dict[str, Any] = {"role": m.role}
            # Multimodal: content_parts takes precedence
            if m.content_parts:
                d["content"] = OpenAIAdapter._convert_content_parts(m.content_parts)
            elif m.content is not None:
                d["content"] = m.content
            if m.tool_calls:
                d["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function_name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in m.tool_calls
                ]
            if m.tool_call_id is not None:
                d["tool_call_id"] = m.tool_call_id
            if m.name is not None:
                d["name"] = m.name
            result.append(d)
        return result

    @staticmethod
    def _convert_content_parts(parts: list[ContentPart]) -> list[dict[str, Any]]:
        """Convert framework ContentParts to OpenAI content array format."""
        result: list[dict[str, Any]] = []
        for p in parts:
            if p.type == "text":
                result.append({"type": "text", "text": p.text or ""})
            elif p.type == "image_url":
                img: dict[str, Any] = {"url": p.image_url or ""}
                if p.detail:
                    img["detail"] = p.detail
                result.append({"type": "image_url", "image_url": img})
            elif p.type == "image_base64":
                media = p.media_type or "image/png"
                url = f"data:{media};base64,{p.data or ''}"
                img = {"url": url}
                if p.detail:
                    img["detail"] = p.detail
                result.append({"type": "image_url", "image_url": img})
            elif p.type == "audio":
                fmt = "wav"
                if p.media_type:
                    fmt = p.media_type.split("/")[-1]  # "audio/mp3" -> "mp3"
                result.append({
                    "type": "input_audio",
                    "input_audio": {"data": p.data or "", "format": fmt},
                })
            elif p.type == "file":
                # Files not directly supported by OpenAI vision — treat as text reference
                result.append({"type": "text", "text": f"[File: {p.file_uri or ''}]"})
        return result

    def _build_kwargs(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        formatted = self._messages_to_dicts(messages)
        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": formatted,
            "temperature": temperature if temperature is not None else self.default_temperature,
            "stream": stream,
        }
        resolved_max = max_tokens or self.max_output_tokens
        if resolved_max is not None:
            kwargs["max_tokens"] = resolved_max
        if tools:
            kwargs["tools"] = tools
        return kwargs

    async def _call_with_retry(self, kwargs: dict[str, Any]) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return await self._client.chat.completions.create(**kwargs)
            except self._openai.RateLimitError as exc:
                last_exc = LLMRateLimitError(str(exc))
                logger.warning("Rate limit hit (attempt %d/%d)", attempt + 1, self.max_retries)
            except self._openai.AuthenticationError as exc:
                raise LLMAuthError(str(exc)) from exc
            except self._openai.APITimeoutError as exc:
                last_exc = LLMTimeoutError(str(exc))
                logger.warning("Timeout (attempt %d/%d)", attempt + 1, self.max_retries)
            except self._openai.APIError as exc:
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
        choice = raw.choices[0]
        message = choice.message

        content: str | None = getattr(message, "content", None)
        tool_calls = self._parse_tool_calls(getattr(message, "tool_calls", None))

        usage = getattr(raw, "usage", None)
        token_usage = TokenUsage()
        if usage is not None:
            token_usage = TokenUsage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage, "total_tokens", 0) or 0,
            )

        finish = getattr(choice, "finish_reason", "stop") or "stop"
        if tool_calls and finish == "stop":
            finish = "tool_calls"
        if finish not in ("stop", "tool_calls", "length", "error"):
            finish = "stop"

        raw_meta: dict[str, Any] = {}
        if hasattr(raw, "model"):
            raw_meta["model"] = raw.model
        if hasattr(raw, "id"):
            raw_meta["response_id"] = raw.id

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=token_usage,
            raw_response_meta=raw_meta or None,
        )

    @staticmethod
    def _parse_tool_calls(raw_tool_calls: list[Any] | None) -> list[ToolCallRequest]:
        if not raw_tool_calls:
            return []
        parsed: list[ToolCallRequest] = []
        for tc in raw_tool_calls:
            func = tc.function
            name: str = func.name or ""
            args_str: str = func.arguments or "{}"
            try:
                arguments = json.loads(args_str)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to parse tool args for '%s'", name)
                arguments = {}
            parsed.append(
                ToolCallRequest(id=getattr(tc, "id", ""), function_name=name, arguments=arguments)
            )
        return parsed

    @staticmethod
    def _parse_chunk(chunk: Any) -> ModelChunk:
        choice = chunk.choices[0] if chunk.choices else None
        if choice is None:
            return ModelChunk()
        delta = choice.delta
        delta_content: str | None = getattr(delta, "content", None)
        finish_reason: str | None = getattr(choice, "finish_reason", None)
        raw_tc = getattr(delta, "tool_calls", None)
        delta_tool_calls: list[dict] | None = None
        if raw_tc:
            delta_tool_calls = []
            for tc in raw_tc:
                entry: dict[str, Any] = {"index": getattr(tc, "index", 0)}
                if getattr(tc, "id", None):
                    entry["id"] = tc.id
                func = getattr(tc, "function", None)
                if func:
                    entry["function"] = {
                        "name": getattr(func, "name", None),
                        "arguments": getattr(func, "arguments", ""),
                    }
                delta_tool_calls.append(entry)
        return ModelChunk(
            delta_content=delta_content,
            delta_tool_calls=delta_tool_calls,
            finish_reason=finish_reason,
        )
