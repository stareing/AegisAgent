"""Google Gemini adapter for the agent framework.

Uses the official `google-genai` SDK. Install with:
    pip install agent-framework[google]

Key differences from OpenAI format:
- Roles: "model" instead of "assistant", no "system" role in messages
- System instruction is passed via config parameter
- Tool calls use function_call parts, results use function_response parts
- Tool schemas wrapped in function_declarations
- Gemini does not provide tool call IDs — adapter generates synthetic ones
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
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


class GoogleAdapter(BaseModelAdapter):
    """Model adapter backed by the Google GenAI SDK (Gemini)."""

    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        timeout_ms: int = 60_000,
        max_retries: int = 3,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> None:
        super().__init__()
        from google import genai

        self.model_name = model_name
        self.max_retries = max_retries
        self.default_temperature = temperature
        self.max_output_tokens = max_output_tokens

        client_kwargs: dict[str, Any] = {}
        if api_key:
            client_kwargs["api_key"] = api_key

        self._client = genai.Client(**client_kwargs)
        self._genai = genai

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
        system_instruction, contents = self._convert_messages(messages)
        config = self._build_config(
            tools=tools, temperature=temperature, max_tokens=max_tokens,
            system_instruction=system_instruction,
        )
        raw = await self._call_with_retry(contents, config)
        return self._parse_response(raw)

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ModelChunk]:
        system_instruction, contents = self._convert_messages(messages)
        config = self._build_config(
            tools=tools, system_instruction=system_instruction,
        )

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                async for chunk in self._client.aio.models.generate_content_stream(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                ):
                    yield self._parse_chunk(chunk)
                return
            except Exception as exc:
                last_exc = self._map_error(exc)
                if isinstance(last_exc, LLMAuthError):
                    raise last_exc from exc
                logger.warning("Stream error (attempt %d/%d)", attempt + 1, self.max_retries)
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(min(2 ** attempt + random.random(), 30.0))

        if last_exc:
            raise last_exc

    def count_tokens(self, messages: list[Message]) -> int:
        # Use char-based estimate. The SDK's count_tokens is async and requires
        # full message conversion, so we keep it simple for synchronous interface.
        text = "".join((m.content or "") for m in messages)
        return len(text) // 4

    def supports_parallel_tool_calls(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Message & tool conversion (Gemini format)
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_messages(
        messages: list[Message],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert framework Messages to Gemini content format.

        Returns (system_instruction, contents).

        Key conversions:
        - system messages → extracted as system_instruction string
        - assistant → role="model"
        - tool messages → user messages with function_response parts
        - assistant tool_calls → model messages with function_call parts
        """
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []

        for m in messages:
            if m.role == "system":
                if m.content:
                    system_parts.append(m.content)
                continue

            if m.role == "assistant":
                parts: list[dict[str, Any]] = []
                if m.content:
                    parts.append({"text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        parts.append({
                            "function_call": {
                                "name": tc.function_name,
                                "args": tc.arguments,
                            }
                        })
                if not parts:
                    parts.append({"text": ""})
                contents.append({"role": "model", "parts": parts})

            elif m.role == "tool":
                # Gemini: tool results are user messages with function_response parts
                fn_response = {
                    "function_response": {
                        "name": m.name or "",
                        "response": {"result": m.content or ""},
                    }
                }
                # Merge consecutive tool results into one user message
                if contents and contents[-1]["role"] == "user" and any(
                    "function_response" in p for p in contents[-1]["parts"]
                ):
                    contents[-1]["parts"].append(fn_response)
                else:
                    contents.append({"role": "user", "parts": [fn_response]})

            elif m.role == "user":
                if m.content_parts:
                    parts = GoogleAdapter._convert_content_parts(m.content_parts)
                    contents.append({"role": "user", "parts": parts})
                else:
                    contents.append({
                        "role": "user",
                        "parts": [{"text": m.content or ""}],
                    })

        system_instruction = "\n\n".join(system_parts) if system_parts else None
        return system_instruction, contents

    @staticmethod
    def _convert_content_parts(parts: list) -> list[dict[str, Any]]:
        """Convert framework ContentParts to Gemini parts format."""
        result: list[dict[str, Any]] = []
        for p in parts:
            if p.type == "text":
                result.append({"text": p.text or ""})
            elif p.type == "image_url":
                # Gemini: use file_data with URL
                result.append({
                    "file_data": {"mime_type": "image/jpeg", "file_uri": p.image_url or ""},
                })
            elif p.type == "image_base64":
                result.append({
                    "inline_data": {
                        "mime_type": p.media_type or "image/png",
                        "data": p.data or "",
                    },
                })
            elif p.type == "audio":
                result.append({
                    "inline_data": {
                        "mime_type": p.media_type or "audio/wav",
                        "data": p.data or "",
                    },
                })
            elif p.type == "file":
                mime = p.media_type or "application/octet-stream"
                result.append({
                    "file_data": {"mime_type": mime, "file_uri": p.file_uri or ""},
                })
        return result

    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict[str, Any]]:
        """Convert OpenAI-format tool schemas to Gemini format.

        OpenAI:  {"type": "function", "function": {"name", "description", "parameters"}}
        Gemini:  {"function_declarations": [{"name", "description", "parameters"}]}
        """
        declarations: list[dict[str, Any]] = []
        for t in tools:
            func = t.get("function", t)
            declarations.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "parameters": func.get("parameters", {"type": "object", "properties": {}}),
            })
        return [{"function_declarations": declarations}]

    # ------------------------------------------------------------------
    # Build config & retry
    # ------------------------------------------------------------------

    def _build_config(
        self,
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system_instruction: str | None = None,
    ) -> dict[str, Any]:
        """Build the GenerateContentConfig dict."""
        config: dict[str, Any] = {
            "temperature": temperature if temperature is not None else self.default_temperature,
        }
        resolved_max = max_tokens or self.max_output_tokens
        if resolved_max is not None:
            config["max_output_tokens"] = resolved_max
        if system_instruction:
            config["system_instruction"] = system_instruction
        if tools:
            config["tools"] = self._convert_tools(tools)
        return config

    async def _call_with_retry(
        self, contents: list[dict], config: dict[str, Any],
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return await self._client.aio.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                last_exc = self._map_error(exc)
                if isinstance(last_exc, LLMAuthError):
                    raise last_exc from exc
                logger.warning(
                    "Gemini API error (attempt %d/%d): %s", attempt + 1, self.max_retries, exc,
                )

            if attempt < self.max_retries - 1:
                backoff = min(2 ** attempt + random.random(), 30.0)
                await asyncio.sleep(backoff)

        raise last_exc  # type: ignore[misc]

    def _map_error(self, exc: Exception) -> LLMCallError:
        """Map google-genai exceptions to framework exceptions."""
        exc_str = str(exc).lower()
        # google-genai raises google.genai.errors.ClientError / ServerError
        # Inspect status codes or message patterns
        if "429" in exc_str or "rate" in exc_str or "quota" in exc_str:
            return LLMRateLimitError(str(exc))
        if "401" in exc_str or "403" in exc_str or "auth" in exc_str or "permission" in exc_str:
            return LLMAuthError(str(exc))
        if "timeout" in exc_str or "deadline" in exc_str:
            return LLMTimeoutError(str(exc))
        return LLMCallError(str(exc))

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, raw: Any) -> ModelResponse:
        """Convert Gemini response to ModelResponse.

        Gemini response.candidates[0].content.parts contains:
        - text parts: {"text": "..."}
        - function_call parts: {"function_call": {"name": "...", "args": {...}}}
        """
        candidate = raw.candidates[0] if raw.candidates else None
        if candidate is None:
            return ModelResponse(content=None, finish_reason="error")

        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []

        parts = getattr(getattr(candidate, "content", None), "parts", []) or []
        for part in parts:
            if hasattr(part, "text") and part.text:
                text_parts.append(part.text)
            if hasattr(part, "function_call") and part.function_call:
                fc = part.function_call
                # Gemini does not provide tool call IDs — generate synthetic ones
                tool_calls.append(
                    ToolCallRequest(
                        id=f"call_{uuid.uuid4().hex[:8]}",
                        function_name=getattr(fc, "name", ""),
                        arguments=dict(getattr(fc, "args", {})) if getattr(fc, "args", None) else {},
                    )
                )

        content = "\n".join(text_parts) if text_parts else None

        # Map finish reason
        finish_reason_raw = getattr(candidate, "finish_reason", None)
        if tool_calls:
            finish = "tool_calls"
        elif finish_reason_raw and "MAX_TOKENS" in str(finish_reason_raw):
            finish = "length"
        else:
            finish = "stop"

        # Token usage
        usage_meta = getattr(raw, "usage_metadata", None)
        token_usage = TokenUsage()
        if usage_meta:
            prompt = getattr(usage_meta, "prompt_token_count", 0) or 0
            completion = getattr(usage_meta, "candidates_token_count", 0) or 0
            token_usage = TokenUsage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
            )

        raw_meta: dict[str, Any] = {"model": self.model_name}

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=token_usage,
            raw_response_meta=raw_meta,
        )

    @staticmethod
    def _parse_chunk(chunk: Any) -> ModelChunk:
        """Convert a Gemini streaming chunk to ModelChunk."""
        candidate = chunk.candidates[0] if getattr(chunk, "candidates", None) else None
        if candidate is None:
            return ModelChunk()

        parts = getattr(getattr(candidate, "content", None), "parts", []) or []
        delta_content: str | None = None
        delta_tool_calls: list[dict] | None = None

        for part in parts:
            if hasattr(part, "text") and part.text:
                delta_content = (delta_content or "") + part.text
            if hasattr(part, "function_call") and part.function_call:
                fc = part.function_call
                if delta_tool_calls is None:
                    delta_tool_calls = []
                delta_tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "function": {
                        "name": getattr(fc, "name", ""),
                        "arguments": json.dumps(dict(getattr(fc, "args", {})) if getattr(fc, "args", None) else {}),
                    },
                })

        finish_reason: str | None = None
        fr_raw = getattr(candidate, "finish_reason", None)
        if fr_raw:
            finish_reason = "stop"

        return ModelChunk(
            delta_content=delta_content,
            delta_tool_calls=delta_tool_calls,
            finish_reason=finish_reason,
        )
