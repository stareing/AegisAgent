"""OpenAI-compatible adapters for Chinese AI providers and custom endpoints.

All these providers implement the OpenAI Chat Completions API format,
so they share the same message/tool conversion logic from OpenAIAdapter.
Differences are primarily: base_url, default model names, minor quirks.

Supported providers:
- DeepSeek  (https://api.deepseek.com)
- Doubao/豆包 (Volcengine Ark — https://ark.cn-beijing.volces.com/api/v3)
- Qwen/通义千问 (DashScope — https://dashscope.aliyuncs.com/compatible-mode/v1)
- Zhipu/智谱GLM (https://open.bigmodel.cn/api/paas/v4)
- MiniMax  (https://api.minimax.chat/v1)
- Custom   (user-specified endpoint)

Install with:
    pip install agent-framework[openai]    # all use the openai SDK
"""

from __future__ import annotations

import logging
from typing import Any

from agent_framework.adapters.model.base_adapter import (
    BaseModelAdapter,
    ModelChunk,
)
from agent_framework.models.message import Message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared OpenAI-format response parsing (avoids coupling to OpenAIAdapter instance)
# ---------------------------------------------------------------------------


def _parse_openai_response(raw: Any) -> Any:
    """Parse an OpenAI-format API response into ModelResponse."""
    import json

    from agent_framework.models.message import ModelResponse, TokenUsage, ToolCallRequest

    choice = raw.choices[0]
    message = choice.message

    content: str | None = getattr(message, "content", None)

    # Parse tool calls
    raw_tool_calls = getattr(message, "tool_calls", None)
    tool_calls: list[ToolCallRequest] = []
    if raw_tool_calls:
        for tc in raw_tool_calls:
            func = tc.function
            name: str = func.name or ""
            args_str: str = func.arguments or "{}"
            try:
                arguments = json.loads(args_str)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            tool_calls.append(
                ToolCallRequest(id=getattr(tc, "id", ""), function_name=name, arguments=arguments)
            )

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


# ---------------------------------------------------------------------------
# Provider default configurations
# ---------------------------------------------------------------------------

PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "api_base": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
    },
    "doubao": {
        "api_base": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "doubao-pro-32k",
    },
    "qwen": {
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
    },
    "zhipu": {
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4",
    },
    "minimax": {
        "api_base": "https://api.minimax.chat/v1",
        "default_model": "abab6.5s-chat",
    },
}


# ---------------------------------------------------------------------------
# Base: OpenAI-compatible adapter
# ---------------------------------------------------------------------------


class OpenAICompatibleAdapter(BaseModelAdapter):
    """Adapter for any OpenAI-compatible API endpoint.

    Uses the official ``openai`` SDK with a custom ``base_url``.
    All Chinese LLM providers that expose a /chat/completions endpoint
    can be accessed through this adapter.
    """

    # Subclasses can set a provider key to auto-resolve defaults.
    _provider: str | None = None

    def __init__(
        self,
        model_name: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        timeout_ms: int = 60_000,
        max_retries: int = 3,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        import openai

        # Resolve provider defaults
        defaults = PROVIDER_DEFAULTS.get(self._provider or "", {})
        resolved_base = api_base or defaults.get("api_base")
        resolved_model = model_name or defaults.get("default_model", "gpt-3.5-turbo")

        if not resolved_base:
            raise ValueError(
                "api_base is required for OpenAICompatibleAdapter. "
                "Either pass it explicitly or use a provider subclass."
            )

        self.model_name = resolved_model
        self.max_retries = max_retries
        self.default_temperature = temperature
        self.max_output_tokens = max_output_tokens
        self._extra_headers = extra_headers

        client_kwargs: dict[str, Any] = {
            "api_key": api_key or "EMPTY",
            "base_url": resolved_base,
            "timeout": timeout_ms / 1000.0,
            "max_retries": 0,  # we handle retries ourselves
        }
        if extra_headers:
            client_kwargs["default_headers"] = extra_headers

        self._client = openai.AsyncOpenAI(**client_kwargs)
        self._openai = openai

        logger.info(
            "OpenAICompatibleAdapter initialized: provider=%s model=%s base=%s",
            self._provider or "custom",
            self.model_name,
            resolved_base,
        )

    # ------------------------------------------------------------------
    # Delegate to the shared OpenAI-format implementation
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ):
        kwargs = self._build_kwargs(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens,
        )
        raw = await self._call_with_retry(kwargs)
        return self._parse_response(raw)

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
    ):
        kwargs = self._build_kwargs(messages, tools=tools, stream=True)
        raw_stream = await self._call_with_retry(kwargs)
        async for chunk in raw_stream:
            yield self._parse_chunk(chunk)

    def count_tokens(self, messages: list[Message]) -> int:
        """Rough token estimate. Chinese text averages ~1.5 tokens/char."""
        total = 0
        for m in messages:
            if m.content:
                # Heuristic: ASCII ~4 chars/token, CJK ~1.5 chars/token
                ascii_chars = sum(1 for c in m.content if ord(c) < 128)
                cjk_chars = len(m.content) - ascii_chars
                total += ascii_chars // 4 + int(cjk_chars / 1.5)
            if m.tool_calls:
                for tc in m.tool_calls:
                    import json
                    total += len(json.dumps(tc.arguments)) // 4
            total += 4  # message overhead
        return max(total, 1)

    def supports_parallel_tool_calls(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Internal — mirrors OpenAIAdapter with minor adjustments
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Response parsing — delegates to OpenAIAdapter static/class methods
    # ------------------------------------------------------------------

    def _parse_response(self, raw: Any):
        from agent_framework.adapters.model.openai_adapter import OpenAIAdapter
        # Create a temporary OpenAIAdapter-like context for parsing
        return _parse_openai_response(raw)

    @staticmethod
    def _parse_chunk(chunk: Any) -> ModelChunk:
        from agent_framework.adapters.model.openai_adapter import OpenAIAdapter
        return OpenAIAdapter._parse_chunk(chunk)

    # ------------------------------------------------------------------
    # Request building
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
        from agent_framework.adapters.model.openai_adapter import OpenAIAdapter

        formatted = OpenAIAdapter._messages_to_dicts(messages)
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
        import asyncio
        import random

        from agent_framework.adapters.model.base_adapter import (
            LLMAuthError,
            LLMCallError,
            LLMRateLimitError,
            LLMTimeoutError,
        )

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return await self._client.chat.completions.create(**kwargs)
            except self._openai.RateLimitError as exc:
                last_exc = LLMRateLimitError(str(exc))
                logger.warning("Rate limit (attempt %d/%d)", attempt + 1, self.max_retries)
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


# ---------------------------------------------------------------------------
# Provider-specific adapters
# ---------------------------------------------------------------------------


class DeepSeekAdapter(OpenAICompatibleAdapter):
    """DeepSeek (https://api.deepseek.com).

    Models: deepseek-chat, deepseek-coder, deepseek-reasoner
    """

    _provider = "deepseek"


class DoubaoAdapter(OpenAICompatibleAdapter):
    """Doubao/豆包 by ByteDance (Volcengine Ark platform).

    Models: doubao-pro-32k, doubao-pro-128k, doubao-lite-32k
    Note: model_name is typically an endpoint ID (ep-xxx) on Volcengine.
    """

    _provider = "doubao"


class QwenAdapter(OpenAICompatibleAdapter):
    """Qwen/通义千问 by Alibaba (DashScope OpenAI-compatible mode).

    Models: qwen-plus, qwen-turbo, qwen-max, qwen-long
    API key: DashScope API key (DASHSCOPE_API_KEY).
    """

    _provider = "qwen"


class ZhipuAdapter(OpenAICompatibleAdapter):
    """Zhipu/智谱 GLM (https://open.bigmodel.cn).

    Models: glm-4, glm-4-flash, glm-4-air, glm-4-plus
    """

    _provider = "zhipu"


class MiniMaxAdapter(OpenAICompatibleAdapter):
    """MiniMax (https://api.minimax.chat).

    Models: abab6.5s-chat, abab6.5-chat, abab5.5-chat
    """

    _provider = "minimax"


class CustomAdapter(OpenAICompatibleAdapter):
    """Custom OpenAI-compatible endpoint.

    Use this for any provider that implements the OpenAI Chat Completions API.
    Both api_key and api_base are required.
    """

    _provider = None  # no defaults — user must supply everything
