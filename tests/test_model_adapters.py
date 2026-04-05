"""Strict unit tests for OpenAI / Anthropic / Google model adapters.

Tests cover:
- Message format conversion (framework ↔ provider)
- Tool schema format conversion
- Response parsing (content, tool_calls, finish_reason, usage)
- Streaming chunk parsing
- Error handling & retry logic
- Token counting
- Edge cases (malformed JSON, empty responses, consecutive messages)
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_framework.adapters.model.base_adapter import \
    BaseModelAdapter as _BaseModelAdapter
from agent_framework.adapters.model.base_adapter import (LLMAuthError,
                                                         LLMCallError,
                                                         LLMRateLimitError,
                                                         LLMTimeoutError,
                                                         ModelChunk)
from agent_framework.adapters.model.fallback_adapter import \
    FallbackModelAdapter
from agent_framework.models.message import (Message, ModelResponse, TokenUsage,
                                            ToolCallRequest)

# =====================================================================
# Fixtures: Shared tool schemas & messages
# =====================================================================

SAMPLE_OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Calculate math expressions",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
    }
]

SAMPLE_MESSAGES = [
    Message(role="system", content="You are a helpful assistant."),
    Message(role="user", content="What is 2+2?"),
]

SAMPLE_MESSAGES_WITH_TOOL_CALL = [
    Message(role="system", content="You are a helpful assistant."),
    Message(role="user", content="What is 2+2?"),
    Message(
        role="assistant",
        content=None,
        tool_calls=[
            ToolCallRequest(id="call_abc123", function_name="calculator", arguments={"expression": "2+2"})
        ],
    ),
    Message(role="tool", content="4", tool_call_id="call_abc123", name="calculator"),
]


# =====================================================================
# OpenAI Adapter Tests
# =====================================================================


class TestOpenAIAdapterMessageConversion:
    """Test OpenAI message format conversion."""

    def _get_adapter_class(self):
        with patch("openai.AsyncOpenAI"):
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter
            return OpenAIAdapter

    def test_simple_messages_to_dicts(self):
        cls = self._get_adapter_class()
        result = cls._messages_to_dicts(SAMPLE_MESSAGES)

        assert len(result) == 2
        assert result[0] == {"role": "system", "content": "You are a helpful assistant."}
        assert result[1] == {"role": "user", "content": "What is 2+2?"}

    def test_assistant_message_with_tool_calls(self):
        cls = self._get_adapter_class()
        msg = Message(
            role="assistant",
            content="Let me calculate.",
            tool_calls=[
                ToolCallRequest(id="call_001", function_name="calculator", arguments={"expr": "1+1"})
            ],
        )
        result = cls._messages_to_dicts([msg])

        assert len(result) == 1
        d = result[0]
        assert d["role"] == "assistant"
        assert d["content"] == "Let me calculate."
        assert len(d["tool_calls"]) == 1
        tc = d["tool_calls"][0]
        assert tc["id"] == "call_001"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "calculator"
        assert json.loads(tc["function"]["arguments"]) == {"expr": "1+1"}

    def test_tool_result_message(self):
        cls = self._get_adapter_class()
        msg = Message(role="tool", content="42", tool_call_id="call_xyz", name="calculator")
        result = cls._messages_to_dicts([msg])

        assert len(result) == 1
        d = result[0]
        assert d["role"] == "tool"
        assert d["content"] == "42"
        assert d["tool_call_id"] == "call_xyz"
        assert d["name"] == "calculator"

    def test_none_content_not_included(self):
        cls = self._get_adapter_class()
        msg = Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCallRequest(id="c1", function_name="f", arguments={})],
        )
        result = cls._messages_to_dicts([msg])
        assert "content" not in result[0]


class TestOpenAIAdapterResponseParsing:
    """Test OpenAI response parsing."""

    def _make_adapter(self):
        with patch("openai.AsyncOpenAI"):
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter
            return OpenAIAdapter(model_name="gpt-4o")

    def _make_raw_response(
        self,
        content="Hello",
        tool_calls=None,
        finish_reason="stop",
        prompt_tokens=10,
        completion_tokens=5,
        model="gpt-4o",
    ):
        message = MagicMock()
        message.content = content
        message.tool_calls = tool_calls

        choice = MagicMock()
        choice.message = message
        choice.finish_reason = finish_reason

        usage = MagicMock()
        usage.prompt_tokens = prompt_tokens
        usage.completion_tokens = completion_tokens
        usage.total_tokens = prompt_tokens + completion_tokens

        raw = MagicMock()
        raw.choices = [choice]
        raw.usage = usage
        raw.model = model
        raw.id = "resp_123"
        return raw

    def test_parse_simple_text_response(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response(content="Hello world")
        result = adapter._parse_response(raw)

        assert isinstance(result, ModelResponse)
        assert result.content == "Hello world"
        assert result.finish_reason == "stop"
        assert result.tool_calls == []
        assert result.usage.prompt_tokens == 10
        assert result.usage.completion_tokens == 5
        assert result.usage.total_tokens == 15
        assert result.raw_response_meta["model"] == "gpt-4o"
        assert result.raw_response_meta["response_id"] == "resp_123"

    def test_parse_response_with_tool_calls(self):
        adapter = self._make_adapter()

        tc = MagicMock()
        tc.id = "call_abc"
        tc.function = MagicMock()
        tc.function.name = "calculator"
        tc.function.arguments = '{"expression": "2+2"}'

        raw = self._make_raw_response(content=None, tool_calls=[tc], finish_reason="stop")
        result = adapter._parse_response(raw)

        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "call_abc"
        assert result.tool_calls[0].function_name == "calculator"
        assert result.tool_calls[0].arguments == {"expression": "2+2"}

    def test_parse_tool_calls_malformed_json(self):
        adapter = self._make_adapter()

        tc = MagicMock()
        tc.id = "call_bad"
        tc.function = MagicMock()
        tc.function.name = "broken_tool"
        tc.function.arguments = "not valid json{{"

        result = adapter._parse_tool_calls([tc])

        assert len(result) == 1
        assert result[0].function_name == "broken_tool"
        assert result[0].arguments == {}

    def test_parse_response_length_truncated(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response(finish_reason="length")
        result = adapter._parse_response(raw)
        assert result.finish_reason == "length"

    def test_parse_response_no_usage(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response()
        raw.usage = None
        result = adapter._parse_response(raw)
        assert result.usage.total_tokens == 0

    def test_parse_chunk_with_content(self):
        from agent_framework.adapters.model.openai_adapter import OpenAIAdapter

        delta = MagicMock()
        delta.content = "Hello"
        delta.tool_calls = None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = None

        chunk = MagicMock()
        chunk.choices = [choice]

        result = OpenAIAdapter._parse_chunk(chunk)

        assert isinstance(result, ModelChunk)
        assert result.delta_content == "Hello"
        assert result.finish_reason is None

    def test_parse_chunk_empty_choices(self):
        from agent_framework.adapters.model.openai_adapter import OpenAIAdapter

        chunk = MagicMock()
        chunk.choices = []
        result = OpenAIAdapter._parse_chunk(chunk)
        assert result.delta_content is None

    def test_parse_chunk_with_tool_call_delta(self):
        from agent_framework.adapters.model.openai_adapter import OpenAIAdapter

        tc_delta = MagicMock()
        tc_delta.index = 0
        tc_delta.id = "call_stream_1"
        tc_delta.function = MagicMock()
        tc_delta.function.name = "calculator"
        tc_delta.function.arguments = '{"x":'

        delta = MagicMock()
        delta.content = None
        delta.tool_calls = [tc_delta]

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = None

        chunk = MagicMock()
        chunk.choices = [choice]

        result = OpenAIAdapter._parse_chunk(chunk)
        assert result.delta_tool_calls is not None
        assert len(result.delta_tool_calls) == 1
        assert result.delta_tool_calls[0]["id"] == "call_stream_1"
        assert result.delta_tool_calls[0]["function"]["name"] == "calculator"


class TestOpenAIAdapterBuildKwargs:
    """Test OpenAI kwargs construction."""

    def _make_adapter(self, **kw):
        with patch("openai.AsyncOpenAI"):
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter
            return OpenAIAdapter(model_name="gpt-4o", **kw)

    def test_basic_kwargs(self):
        adapter = self._make_adapter()
        kwargs = adapter._build_kwargs(SAMPLE_MESSAGES)

        assert kwargs["model"] == "gpt-4o"
        assert kwargs["temperature"] == 0.0
        assert kwargs["stream"] is False
        assert "tools" not in kwargs
        assert "max_tokens" not in kwargs

    def test_kwargs_with_tools(self):
        adapter = self._make_adapter()
        kwargs = adapter._build_kwargs(SAMPLE_MESSAGES, tools=SAMPLE_OPENAI_TOOLS)
        assert kwargs["tools"] == SAMPLE_OPENAI_TOOLS

    def test_kwargs_with_max_tokens_override(self):
        adapter = self._make_adapter(max_output_tokens=1000)
        kwargs = adapter._build_kwargs(SAMPLE_MESSAGES, max_tokens=2000)
        assert kwargs["max_tokens"] == 2000

    def test_kwargs_fallback_to_default_max_tokens(self):
        adapter = self._make_adapter(max_output_tokens=1000)
        kwargs = adapter._build_kwargs(SAMPLE_MESSAGES)
        assert kwargs["max_tokens"] == 1000

    def test_kwargs_temperature_override(self):
        adapter = self._make_adapter(temperature=0.5)
        kwargs = adapter._build_kwargs(SAMPLE_MESSAGES, temperature=0.9)
        assert kwargs["temperature"] == 0.9

    def test_kwargs_stream_mode(self):
        adapter = self._make_adapter()
        kwargs = adapter._build_kwargs(SAMPLE_MESSAGES, stream=True)
        assert kwargs["stream"] is True


class TestOpenAIAdapterRetryAndErrors:
    """Test OpenAI retry logic and error mapping."""

    @pytest.mark.asyncio
    async def test_auth_error_raises_immediately(self):
        import openai as openai_module

        with patch("openai.AsyncOpenAI") as MockClient:
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter

            adapter = OpenAIAdapter(model_name="gpt-4o", max_retries=3)
            mock_create = AsyncMock(
                side_effect=openai_module.AuthenticationError(
                    message="Invalid API key",
                    response=MagicMock(status_code=401),
                    body=None,
                )
            )
            adapter._client.chat.completions.create = mock_create

            with pytest.raises(LLMAuthError, match="Invalid API key"):
                await adapter._call_with_retry({"model": "gpt-4o", "messages": []})

            assert mock_create.call_count == 1  # no retries for auth errors

    @pytest.mark.asyncio
    async def test_rate_limit_retries_then_raises(self):
        import openai as openai_module

        with patch("openai.AsyncOpenAI"):
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter

            adapter = OpenAIAdapter(model_name="gpt-4o", max_retries=2)
            mock_create = AsyncMock(
                side_effect=openai_module.RateLimitError(
                    message="Rate limited",
                    response=MagicMock(status_code=429),
                    body=None,
                )
            )
            adapter._client.chat.completions.create = mock_create

            with pytest.raises(LLMRateLimitError):
                await adapter._call_with_retry({"model": "gpt-4o", "messages": []})

            assert mock_create.call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_retries_then_raises(self):
        import openai as openai_module

        with patch("openai.AsyncOpenAI"):
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter

            adapter = OpenAIAdapter(model_name="gpt-4o", max_retries=2)
            mock_create = AsyncMock(
                side_effect=openai_module.APITimeoutError(request=MagicMock())
            )
            adapter._client.chat.completions.create = mock_create

            with pytest.raises(LLMTimeoutError):
                await adapter._call_with_retry({"model": "gpt-4o", "messages": []})

            assert mock_create.call_count == 2

    @pytest.mark.asyncio
    async def test_generic_api_error_retries(self):
        import openai as openai_module

        with patch("openai.AsyncOpenAI"):
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter

            adapter = OpenAIAdapter(model_name="gpt-4o", max_retries=2)
            mock_create = AsyncMock(
                side_effect=openai_module.APIError(
                    message="Server error",
                    request=MagicMock(),
                    body=None,
                )
            )
            adapter._client.chat.completions.create = mock_create

            with pytest.raises(LLMCallError):
                await adapter._call_with_retry({"model": "gpt-4o", "messages": []})

            assert mock_create.call_count == 2

    @pytest.mark.asyncio
    async def test_successful_complete(self):
        with patch("openai.AsyncOpenAI"):
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter

            adapter = OpenAIAdapter(model_name="gpt-4o", max_retries=1)

            # Build a mock response
            message = MagicMock()
            message.content = "The answer is 4."
            message.tool_calls = None
            choice = MagicMock()
            choice.message = message
            choice.finish_reason = "stop"
            usage = MagicMock()
            usage.prompt_tokens = 10
            usage.completion_tokens = 8
            usage.total_tokens = 18
            raw = MagicMock()
            raw.choices = [choice]
            raw.usage = usage
            raw.model = "gpt-4o"
            raw.id = "resp_001"

            adapter._client.chat.completions.create = AsyncMock(return_value=raw)

            result = await adapter.complete(SAMPLE_MESSAGES)

            assert result.content == "The answer is 4."
            assert result.finish_reason == "stop"
            assert result.usage.total_tokens == 18

    def test_supports_parallel_tool_calls(self):
        with patch("openai.AsyncOpenAI"):
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter
            adapter = OpenAIAdapter(model_name="gpt-4o")
            assert adapter.supports_parallel_tool_calls() is True


class TestOpenAIAdapterTokenCounting:
    """Test OpenAI token counting."""

    def test_count_tokens_fallback(self):
        with patch("openai.AsyncOpenAI"):
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter
            adapter = OpenAIAdapter(model_name="unknown-model-xyz")
            # With an unknown model, tiktoken may raise; fallback to char estimate
            count = adapter.count_tokens([Message(role="user", content="Hello world")])
            assert count > 0

    def test_count_tokens_empty_messages(self):
        with patch("openai.AsyncOpenAI"):
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter
            adapter = OpenAIAdapter(model_name="gpt-4o")
            count = adapter.count_tokens([])
            assert count == 0


# =====================================================================
# Anthropic Adapter Tests
# =====================================================================


class TestAnthropicAdapterMessageConversion:
    """Test Anthropic message format conversion."""

    def _get_adapter_class(self):
        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter
            return AnthropicAdapter

    def test_extract_system_single(self):
        cls = self._get_adapter_class()
        msgs = [
            Message(role="system", content="Be helpful."),
            Message(role="user", content="Hi"),
        ]
        system, non_system = cls._extract_system(msgs)
        assert system == "Be helpful."
        assert len(non_system) == 1
        assert non_system[0].role == "user"

    def test_extract_system_multiple(self):
        cls = self._get_adapter_class()
        msgs = [
            Message(role="system", content="Rule 1."),
            Message(role="system", content="Rule 2."),
            Message(role="user", content="Hi"),
        ]
        system, non_system = cls._extract_system(msgs)
        assert system == "Rule 1.\n\nRule 2."
        assert len(non_system) == 1

    def test_extract_system_none(self):
        cls = self._get_adapter_class()
        msgs = [Message(role="user", content="Hi")]
        system, non_system = cls._extract_system(msgs)
        assert system is None
        assert len(non_system) == 1

    def test_convert_user_message(self):
        cls = self._get_adapter_class()
        msgs = [Message(role="user", content="Hello")]
        result = cls._convert_messages(msgs)
        assert len(result) == 1
        assert result[0] == {"role": "user", "content": "Hello"}

    def test_convert_assistant_with_text(self):
        cls = self._get_adapter_class()
        msgs = [Message(role="assistant", content="Sure, I can help.")]
        result = cls._convert_messages(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == [{"type": "text", "text": "Sure, I can help."}]

    def test_convert_assistant_with_tool_use(self):
        cls = self._get_adapter_class()
        msgs = [
            Message(
                role="assistant",
                content="Let me calculate.",
                tool_calls=[
                    ToolCallRequest(id="tu_001", function_name="calculator", arguments={"expr": "1+1"})
                ],
            )
        ]
        result = cls._convert_messages(msgs)
        blocks = result[0]["content"]
        assert len(blocks) == 2
        assert blocks[0] == {"type": "text", "text": "Let me calculate."}
        assert blocks[1] == {
            "type": "tool_use",
            "id": "tu_001",
            "name": "calculator",
            "input": {"expr": "1+1"},
        }

    def test_convert_tool_result(self):
        cls = self._get_adapter_class()
        msgs = [Message(role="tool", content="42", tool_call_id="tu_001")]
        result = cls._convert_messages(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert isinstance(result[0]["content"], list)
        block = result[0]["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "tu_001"
        assert block["content"] == "42"

    def test_consecutive_tool_results_merged(self):
        """Anthropic requires consecutive tool results in the same user message."""
        cls = self._get_adapter_class()
        msgs = [
            Message(role="tool", content="result1", tool_call_id="tu_001"),
            Message(role="tool", content="result2", tool_call_id="tu_002"),
        ]
        result = cls._convert_messages(msgs)
        # Should be merged into a single user message
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert len(result[0]["content"]) == 2
        assert result[0]["content"][0]["tool_use_id"] == "tu_001"
        assert result[0]["content"][1]["tool_use_id"] == "tu_002"

    def test_convert_assistant_no_content(self):
        cls = self._get_adapter_class()
        msgs = [Message(role="assistant", content=None)]
        result = cls._convert_messages(msgs)
        # Should have an empty text block (Anthropic requires non-empty content)
        assert result[0]["content"] == [{"type": "text", "text": ""}]

    def test_full_conversation_flow(self):
        """Test a realistic multi-turn conversation with tool calls."""
        cls = self._get_adapter_class()
        result = cls._convert_messages(SAMPLE_MESSAGES_WITH_TOOL_CALL[1:])  # skip system

        assert len(result) == 3  # user, assistant, user(tool_result)
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "user"
        assert result[2]["content"][0]["type"] == "tool_result"


class TestAnthropicAdapterToolConversion:
    """Test Anthropic tool schema conversion."""

    def _get_adapter_class(self):
        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter
            return AnthropicAdapter

    def test_convert_openai_tools_to_anthropic(self):
        cls = self._get_adapter_class()
        result = cls._convert_tools(SAMPLE_OPENAI_TOOLS)

        assert len(result) == 1
        tool = result[0]
        assert tool["name"] == "calculator"
        assert tool["description"] == "Calculate math expressions"
        assert tool["input_schema"]["type"] == "object"
        assert "expression" in tool["input_schema"]["properties"]

    def test_convert_multiple_tools(self):
        cls = self._get_adapter_class()
        tools = SAMPLE_OPENAI_TOOLS + [
            {
                "type": "function",
                "function": {
                    "name": "weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ]
        result = cls._convert_tools(tools)
        assert len(result) == 2
        assert result[0]["name"] == "calculator"
        assert result[1]["name"] == "weather"

    def test_convert_tool_no_parameters(self):
        cls = self._get_adapter_class()
        tools = [{"type": "function", "function": {"name": "noop", "description": "Do nothing"}}]
        result = cls._convert_tools(tools)
        assert result[0]["input_schema"] == {"type": "object", "properties": {}}


class TestAnthropicAdapterResponseParsing:
    """Test Anthropic response parsing."""

    def _make_adapter(self):
        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter
            return AnthropicAdapter(model_name="claude-sonnet-4-20250514")

    def _make_text_block(self, text):
        block = MagicMock()
        block.type = "text"
        block.text = text
        return block

    def _make_tool_use_block(self, id, name, input_data):
        block = MagicMock()
        block.type = "tool_use"
        block.id = id
        block.name = name
        block.input = input_data
        return block

    def _make_raw_response(
        self,
        content_blocks=None,
        stop_reason="end_turn",
        input_tokens=20,
        output_tokens=10,
    ):
        raw = MagicMock()
        raw.content = content_blocks or []
        raw.stop_reason = stop_reason

        usage = MagicMock()
        usage.input_tokens = input_tokens
        usage.output_tokens = output_tokens
        raw.usage = usage

        raw.model = "claude-sonnet-4-20250514"
        raw.id = "msg_resp_001"
        return raw

    def test_parse_text_response(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response(
            content_blocks=[self._make_text_block("Hello!")]
        )
        result = adapter._parse_response(raw)

        assert result.content == "Hello!"
        assert result.finish_reason == "stop"
        assert result.tool_calls == []
        assert result.usage.prompt_tokens == 20
        assert result.usage.completion_tokens == 10
        assert result.usage.total_tokens == 30

    def test_parse_tool_use_response(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response(
            content_blocks=[
                self._make_text_block("Let me calculate."),
                self._make_tool_use_block("tu_001", "calculator", {"expression": "2+2"}),
            ],
            stop_reason="tool_use",
        )
        result = adapter._parse_response(raw)

        assert result.content == "Let me calculate."
        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "tu_001"
        assert result.tool_calls[0].function_name == "calculator"
        assert result.tool_calls[0].arguments == {"expression": "2+2"}

    def test_parse_multiple_tool_calls(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response(
            content_blocks=[
                self._make_tool_use_block("tu_001", "calc", {"x": 1}),
                self._make_tool_use_block("tu_002", "weather", {"city": "Beijing"}),
            ],
            stop_reason="tool_use",
        )
        result = adapter._parse_response(raw)
        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].function_name == "calc"
        assert result.tool_calls[1].function_name == "weather"

    def test_parse_max_tokens_finish(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response(
            content_blocks=[self._make_text_block("Truncated output")],
            stop_reason="max_tokens",
        )
        result = adapter._parse_response(raw)
        assert result.finish_reason == "length"

    def test_parse_empty_content(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response(content_blocks=[])
        result = adapter._parse_response(raw)
        assert result.content is None

    def test_parse_no_usage(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response()
        raw.usage = None
        result = adapter._parse_response(raw)
        assert result.usage.total_tokens == 0


class TestAnthropicAdapterStreamParsing:
    """Test Anthropic streaming event parsing."""

    def _get_adapter_class(self):
        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter
            return AnthropicAdapter

    def test_parse_text_delta(self):
        cls = self._get_adapter_class()
        event = MagicMock()
        event.type = "content_block_delta"
        event.delta = MagicMock()
        event.delta.type = "text_delta"
        event.delta.text = "Hello"

        chunk = cls._parse_stream_event(event)
        assert chunk is not None
        assert chunk.delta_content == "Hello"

    def test_parse_tool_use_start(self):
        cls = self._get_adapter_class()
        event = MagicMock()
        event.type = "content_block_start"
        event.index = 1
        event.content_block = MagicMock()
        event.content_block.type = "tool_use"
        event.content_block.id = "tu_stream_1"
        event.content_block.name = "calculator"

        chunk = cls._parse_stream_event(event)
        assert chunk is not None
        assert chunk.delta_tool_calls is not None
        assert chunk.delta_tool_calls[0]["id"] == "tu_stream_1"
        assert chunk.delta_tool_calls[0]["function"]["name"] == "calculator"

    def test_parse_input_json_delta(self):
        cls = self._get_adapter_class()
        event = MagicMock()
        event.type = "content_block_delta"
        event.index = 1
        event.delta = MagicMock()
        event.delta.type = "input_json_delta"
        event.delta.partial_json = '{"expr":'

        chunk = cls._parse_stream_event(event)
        assert chunk is not None
        assert chunk.delta_tool_calls is not None
        assert chunk.delta_tool_calls[0]["function"]["arguments"] == '{"expr":'

    def test_parse_message_delta_stop(self):
        cls = self._get_adapter_class()
        event = MagicMock()
        event.type = "message_delta"
        event.delta = MagicMock()
        event.delta.stop_reason = "end_turn"

        chunk = cls._parse_stream_event(event)
        assert chunk is not None
        assert chunk.finish_reason == "stop"

    def test_parse_message_delta_tool_use_stop(self):
        cls = self._get_adapter_class()
        event = MagicMock()
        event.type = "message_delta"
        event.delta = MagicMock()
        event.delta.stop_reason = "tool_use"

        chunk = cls._parse_stream_event(event)
        assert chunk.finish_reason == "tool_calls"

    def test_parse_unknown_event_returns_none(self):
        cls = self._get_adapter_class()
        event = MagicMock()
        event.type = "ping"

        chunk = cls._parse_stream_event(event)
        assert chunk is None


class TestAnthropicAdapterRetryAndErrors:
    """Test Anthropic retry logic and error mapping."""

    @pytest.mark.asyncio
    async def test_auth_error_raises_immediately(self):
        import anthropic as anthropic_module

        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter

            adapter = AnthropicAdapter(model_name="claude-sonnet-4-20250514", max_retries=3)
            mock_create = AsyncMock(
                side_effect=anthropic_module.AuthenticationError(
                    message="Invalid key",
                    response=MagicMock(status_code=401),
                    body=None,
                )
            )
            adapter._client.messages.create = mock_create

            with pytest.raises(LLMAuthError):
                await adapter._call_with_retry({"model": "claude-sonnet-4-20250514", "messages": [], "max_tokens": 100})

            assert mock_create.call_count == 1

    @pytest.mark.asyncio
    async def test_rate_limit_retries(self):
        import anthropic as anthropic_module

        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter

            adapter = AnthropicAdapter(model_name="claude-sonnet-4-20250514", max_retries=2)
            mock_create = AsyncMock(
                side_effect=anthropic_module.RateLimitError(
                    message="Rate limited",
                    response=MagicMock(status_code=429),
                    body=None,
                )
            )
            adapter._client.messages.create = mock_create

            with pytest.raises(LLMRateLimitError):
                await adapter._call_with_retry({"model": "claude-sonnet-4-20250514", "messages": [], "max_tokens": 100})

            assert mock_create.call_count == 2

    @pytest.mark.asyncio
    async def test_successful_complete(self):
        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter

            adapter = AnthropicAdapter(model_name="claude-sonnet-4-20250514", max_retries=1)

            text_block = MagicMock()
            text_block.type = "text"
            text_block.text = "The answer is 4."

            raw = MagicMock()
            raw.content = [text_block]
            raw.stop_reason = "end_turn"
            usage = MagicMock()
            usage.input_tokens = 15
            usage.output_tokens = 8
            raw.usage = usage
            raw.model = "claude-sonnet-4-20250514"
            raw.id = "msg_001"

            adapter._client.messages.create = AsyncMock(return_value=raw)

            result = await adapter.complete(SAMPLE_MESSAGES)
            assert result.content == "The answer is 4."
            assert result.finish_reason == "stop"

    def test_supports_parallel_tool_calls(self):
        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter
            adapter = AnthropicAdapter(model_name="claude-sonnet-4-20250514")
            assert adapter.supports_parallel_tool_calls() is True

    def test_count_tokens_estimate(self):
        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter
            adapter = AnthropicAdapter(model_name="claude-sonnet-4-20250514")
            count = adapter.count_tokens([Message(role="user", content="Hello world")])
            assert count > 0


class TestAnthropicAdapterBuildKwargs:
    """Test Anthropic kwargs construction."""

    def _make_adapter(self):
        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter
            return AnthropicAdapter(model_name="claude-sonnet-4-20250514")

    def test_system_extracted_into_kwarg(self):
        adapter = self._make_adapter()
        kwargs = adapter._build_kwargs(SAMPLE_MESSAGES)
        assert kwargs["system"] == "You are a helpful assistant."
        # Messages should not contain system
        for msg in kwargs["messages"]:
            assert msg["role"] != "system"

    def test_no_system_no_kwarg(self):
        adapter = self._make_adapter()
        msgs = [Message(role="user", content="Hi")]
        kwargs = adapter._build_kwargs(msgs)
        assert "system" not in kwargs

    def test_tools_converted(self):
        adapter = self._make_adapter()
        kwargs = adapter._build_kwargs(SAMPLE_MESSAGES, tools=SAMPLE_OPENAI_TOOLS)
        assert "tools" in kwargs
        assert kwargs["tools"][0]["name"] == "calculator"
        assert "input_schema" in kwargs["tools"][0]

    def test_max_tokens_always_present(self):
        """Anthropic requires max_tokens."""
        adapter = self._make_adapter()
        kwargs = adapter._build_kwargs(SAMPLE_MESSAGES)
        assert "max_tokens" in kwargs
        assert kwargs["max_tokens"] == 4096  # default


# =====================================================================
# Google Adapter Tests
# =====================================================================



def _mock_google_genai():
    """Create a mock google.genai module for testing without the SDK installed."""
    mock_genai = MagicMock()
    mock_google = types.ModuleType("google")
    mock_google.genai = mock_genai  # type: ignore[attr-defined]
    sys.modules["google.genai"] = mock_genai
    # Ensure google module is available
    if "google" not in sys.modules:
        sys.modules["google"] = mock_google
    else:
        sys.modules["google"].genai = mock_genai  # type: ignore[attr-defined]
    return mock_genai


def _cleanup_google_genai():
    """Remove mock google.genai from sys.modules."""
    sys.modules.pop("google.genai", None)
    if "google" in sys.modules and hasattr(sys.modules["google"], "genai"):
        try:
            delattr(sys.modules["google"], "genai")
        except (AttributeError, TypeError):
            pass
    # Force reimport of the adapter next time
    sys.modules.pop("agent_framework.adapters.model.google_adapter", None)


class TestGoogleAdapterMessageConversion:
    """Test Google Gemini message format conversion."""

    def setup_method(self):
        _mock_google_genai()

    def teardown_method(self):
        _cleanup_google_genai()

    def _get_adapter_class(self):
        from agent_framework.adapters.model.google_adapter import GoogleAdapter
        return GoogleAdapter

    def test_extract_system_instruction(self):
        cls = self._get_adapter_class()
        system, contents = cls._convert_messages(SAMPLE_MESSAGES)
        assert system == "You are a helpful assistant."
        assert len(contents) == 1
        assert contents[0]["role"] == "user"

    def test_no_system_instruction(self):
        cls = self._get_adapter_class()
        msgs = [Message(role="user", content="Hello")]
        system, contents = cls._convert_messages(msgs)
        assert system is None
        assert len(contents) == 1

    def test_assistant_mapped_to_model_role(self):
        cls = self._get_adapter_class()
        msgs = [Message(role="assistant", content="Sure.")]
        _, contents = cls._convert_messages(msgs)
        assert contents[0]["role"] == "model"
        assert contents[0]["parts"] == [{"text": "Sure."}]

    def test_assistant_with_function_call(self):
        cls = self._get_adapter_class()
        msgs = [
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCallRequest(id="c1", function_name="calculator", arguments={"expr": "1+1"})
                ],
            )
        ]
        _, contents = cls._convert_messages(msgs)
        parts = contents[0]["parts"]
        assert len(parts) == 1
        assert "function_call" in parts[0]
        assert parts[0]["function_call"]["name"] == "calculator"
        assert parts[0]["function_call"]["args"] == {"expr": "1+1"}

    def test_tool_result_as_function_response(self):
        cls = self._get_adapter_class()
        msgs = [Message(role="tool", content="42", name="calculator")]
        _, contents = cls._convert_messages(msgs)
        assert contents[0]["role"] == "user"
        part = contents[0]["parts"][0]
        assert "function_response" in part
        assert part["function_response"]["name"] == "calculator"
        assert part["function_response"]["response"] == {"result": "42"}

    def test_consecutive_tool_results_merged(self):
        cls = self._get_adapter_class()
        msgs = [
            Message(role="tool", content="result1", name="calc"),
            Message(role="tool", content="result2", name="weather"),
        ]
        _, contents = cls._convert_messages(msgs)
        assert len(contents) == 1
        assert len(contents[0]["parts"]) == 2

    def test_assistant_with_text_and_function_call(self):
        cls = self._get_adapter_class()
        msgs = [
            Message(
                role="assistant",
                content="Thinking...",
                tool_calls=[
                    ToolCallRequest(id="c1", function_name="f", arguments={"a": 1})
                ],
            )
        ]
        _, contents = cls._convert_messages(msgs)
        parts = contents[0]["parts"]
        assert len(parts) == 2
        assert parts[0] == {"text": "Thinking..."}
        assert "function_call" in parts[1]

    def test_full_conversation_flow(self):
        cls = self._get_adapter_class()
        system, contents = cls._convert_messages(SAMPLE_MESSAGES_WITH_TOOL_CALL)
        assert system == "You are a helpful assistant."
        # user, model(tool_call), user(function_response)
        assert len(contents) == 3
        assert contents[0]["role"] == "user"
        assert contents[1]["role"] == "model"
        assert contents[2]["role"] == "user"
        assert "function_response" in contents[2]["parts"][0]


class TestGoogleAdapterToolConversion:
    """Test Google Gemini tool schema conversion."""

    def setup_method(self):
        _mock_google_genai()

    def teardown_method(self):
        _cleanup_google_genai()

    def _get_adapter_class(self):
        from agent_framework.adapters.model.google_adapter import GoogleAdapter
        return GoogleAdapter

    def test_convert_openai_tools_to_gemini(self):
        cls = self._get_adapter_class()
        result = cls._convert_tools(SAMPLE_OPENAI_TOOLS)

        assert len(result) == 1
        assert "function_declarations" in result[0]
        decl = result[0]["function_declarations"]
        assert len(decl) == 1
        assert decl[0]["name"] == "calculator"
        assert decl[0]["description"] == "Calculate math expressions"
        assert decl[0]["parameters"]["type"] == "object"

    def test_convert_multiple_tools(self):
        cls = self._get_adapter_class()
        tools = SAMPLE_OPENAI_TOOLS + [
            {
                "type": "function",
                "function": {"name": "search", "description": "Search the web", "parameters": {}},
            }
        ]
        result = cls._convert_tools(tools)
        # All tools go into a single function_declarations array
        assert len(result) == 1
        assert len(result[0]["function_declarations"]) == 2


class TestGoogleAdapterResponseParsing:
    """Test Google Gemini response parsing."""

    def setup_method(self):
        _mock_google_genai()

    def teardown_method(self):
        _cleanup_google_genai()

    def _make_adapter(self):
        from agent_framework.adapters.model.google_adapter import GoogleAdapter
        return GoogleAdapter(model_name="gemini-2.5-flash")

    def _make_text_part(self, text):
        part = MagicMock()
        part.text = text
        part.function_call = None
        return part

    def _make_function_call_part(self, name, args):
        part = MagicMock()
        part.text = None
        fc = MagicMock()
        fc.name = name
        fc.args = args
        part.function_call = fc
        return part

    def _make_raw_response(
        self,
        parts=None,
        finish_reason="STOP",
        prompt_tokens=10,
        candidates_tokens=5,
    ):
        content = MagicMock()
        content.parts = parts or []

        candidate = MagicMock()
        candidate.content = content
        candidate.finish_reason = finish_reason

        usage = MagicMock()
        usage.prompt_token_count = prompt_tokens
        usage.candidates_token_count = candidates_tokens

        raw = MagicMock()
        raw.candidates = [candidate]
        raw.usage_metadata = usage
        return raw

    def test_parse_text_response(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response(parts=[self._make_text_part("Hello!")])
        result = adapter._parse_response(raw)

        assert result.content == "Hello!"
        assert result.finish_reason == "stop"
        assert result.tool_calls == []
        assert result.usage.prompt_tokens == 10
        assert result.usage.completion_tokens == 5

    def test_parse_function_call_response(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response(
            parts=[self._make_function_call_part("calculator", {"expression": "2+2"})],
        )
        result = adapter._parse_response(raw)

        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].function_name == "calculator"
        assert result.tool_calls[0].arguments == {"expression": "2+2"}
        # Synthetic ID should be generated
        assert result.tool_calls[0].id.startswith("call_")

    def test_parse_mixed_text_and_function_call(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response(
            parts=[
                self._make_text_part("Let me help."),
                self._make_function_call_part("weather", {"city": "Tokyo"}),
            ]
        )
        result = adapter._parse_response(raw)
        assert result.content == "Let me help."
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].function_name == "weather"

    def test_parse_max_tokens_finish(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response(
            parts=[self._make_text_part("Cut off")],
            finish_reason="MAX_TOKENS",
        )
        result = adapter._parse_response(raw)
        assert result.finish_reason == "length"

    def test_parse_no_candidates(self):
        adapter = self._make_adapter()
        raw = MagicMock()
        raw.candidates = []
        result = adapter._parse_response(raw)
        assert result.content is None
        assert result.finish_reason == "error"

    def test_parse_no_usage(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response()
        raw.usage_metadata = None
        result = adapter._parse_response(raw)
        assert result.usage.total_tokens == 0

    def test_parse_function_call_none_args(self):
        adapter = self._make_adapter()
        fc_part = MagicMock()
        fc_part.text = None
        fc = MagicMock()
        fc.name = "noop"
        fc.args = None
        fc_part.function_call = fc

        raw = self._make_raw_response(parts=[fc_part])
        result = adapter._parse_response(raw)
        assert result.tool_calls[0].arguments == {}

    def test_synthetic_tool_call_ids_are_unique(self):
        adapter = self._make_adapter()
        raw = self._make_raw_response(
            parts=[
                self._make_function_call_part("f1", {"a": 1}),
                self._make_function_call_part("f2", {"b": 2}),
            ]
        )
        result = adapter._parse_response(raw)
        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].id != result.tool_calls[1].id


class TestGoogleAdapterChunkParsing:
    """Test Google streaming chunk parsing."""

    def setup_method(self):
        _mock_google_genai()

    def teardown_method(self):
        _cleanup_google_genai()

    def _get_adapter_class(self):
        from agent_framework.adapters.model.google_adapter import GoogleAdapter
        return GoogleAdapter

    def test_parse_text_chunk(self):
        cls = self._get_adapter_class()

        text_part = MagicMock()
        text_part.text = "Hello"
        text_part.function_call = None

        content = MagicMock()
        content.parts = [text_part]

        candidate = MagicMock()
        candidate.content = content
        candidate.finish_reason = None

        chunk = MagicMock()
        chunk.candidates = [candidate]

        result = cls._parse_chunk(chunk)
        assert result.delta_content == "Hello"

    def test_parse_function_call_chunk(self):
        cls = self._get_adapter_class()

        fc = MagicMock()
        fc.name = "calc"
        fc.args = {"x": 1}

        fc_part = MagicMock()
        fc_part.text = None
        fc_part.function_call = fc

        content = MagicMock()
        content.parts = [fc_part]

        candidate = MagicMock()
        candidate.content = content
        candidate.finish_reason = None

        chunk = MagicMock()
        chunk.candidates = [candidate]

        result = cls._parse_chunk(chunk)
        assert result.delta_tool_calls is not None
        assert result.delta_tool_calls[0]["function"]["name"] == "calc"

    def test_parse_empty_chunk(self):
        cls = self._get_adapter_class()
        chunk = MagicMock()
        chunk.candidates = None
        result = cls._parse_chunk(chunk)
        assert result.delta_content is None


class TestGoogleAdapterErrorMapping:
    """Test Google error mapping."""

    def setup_method(self):
        _mock_google_genai()

    def teardown_method(self):
        _cleanup_google_genai()

    def _make_adapter(self):
        from agent_framework.adapters.model.google_adapter import GoogleAdapter
        return GoogleAdapter(model_name="gemini-2.5-flash")

    def test_rate_limit_error(self):
        adapter = self._make_adapter()
        exc = Exception("429 Resource has been exhausted (quota)")
        result = adapter._map_error(exc)
        assert isinstance(result, LLMRateLimitError)

    def test_auth_error_401(self):
        adapter = self._make_adapter()
        exc = Exception("401 Unauthorized: invalid API key")
        result = adapter._map_error(exc)
        assert isinstance(result, LLMAuthError)

    def test_auth_error_403(self):
        adapter = self._make_adapter()
        exc = Exception("403 Permission denied")
        result = adapter._map_error(exc)
        assert isinstance(result, LLMAuthError)

    def test_timeout_error(self):
        adapter = self._make_adapter()
        exc = Exception("Deadline exceeded: timeout")
        result = adapter._map_error(exc)
        assert isinstance(result, LLMTimeoutError)

    def test_generic_error(self):
        adapter = self._make_adapter()
        exc = Exception("Something went wrong")
        result = adapter._map_error(exc)
        assert isinstance(result, LLMCallError)
        assert not isinstance(result, LLMRateLimitError)

    @pytest.mark.asyncio
    async def test_auth_error_not_retried(self):
        from agent_framework.adapters.model.google_adapter import GoogleAdapter

        adapter = GoogleAdapter(model_name="gemini-2.5-flash", max_retries=3)
        call_count = 0

        async def mock_generate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise Exception("401 Unauthorized: invalid key")

        adapter._client.aio.models.generate_content = mock_generate

        with pytest.raises(LLMAuthError):
            await adapter._call_with_retry([], {})

        assert call_count == 1  # no retries for auth

    @pytest.mark.asyncio
    async def test_generic_error_retries(self):
        from agent_framework.adapters.model.google_adapter import GoogleAdapter

        adapter = GoogleAdapter(model_name="gemini-2.5-flash", max_retries=2)
        call_count = 0

        async def mock_generate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise Exception("Internal server error 500")

        adapter._client.aio.models.generate_content = mock_generate

        with pytest.raises(LLMCallError):
            await adapter._call_with_retry([], {})

        assert call_count == 2


class TestGoogleAdapterBuildConfig:
    """Test Google config construction."""

    def setup_method(self):
        _mock_google_genai()

    def teardown_method(self):
        _cleanup_google_genai()

    def _make_adapter(self, **kw):
        from agent_framework.adapters.model.google_adapter import GoogleAdapter
        return GoogleAdapter(model_name="gemini-2.5-flash", **kw)

    def test_basic_config(self):
        adapter = self._make_adapter()
        config = adapter._build_config()
        assert config["temperature"] == 0.0
        assert "tools" not in config
        assert "system_instruction" not in config

    def test_config_with_tools(self):
        adapter = self._make_adapter()
        config = adapter._build_config(tools=SAMPLE_OPENAI_TOOLS)
        assert "tools" in config
        assert "function_declarations" in config["tools"][0]

    def test_config_with_system_instruction(self):
        adapter = self._make_adapter()
        config = adapter._build_config(system_instruction="Be concise.")
        assert config["system_instruction"] == "Be concise."

    def test_config_with_max_tokens(self):
        adapter = self._make_adapter(max_output_tokens=2000)
        config = adapter._build_config()
        assert config["max_output_tokens"] == 2000

    def test_config_max_tokens_override(self):
        adapter = self._make_adapter(max_output_tokens=2000)
        config = adapter._build_config(max_tokens=500)
        assert config["max_output_tokens"] == 500

    def test_config_temperature_override(self):
        adapter = self._make_adapter(temperature=0.5)
        config = adapter._build_config(temperature=0.9)
        assert config["temperature"] == 0.9

    def test_supports_parallel_tool_calls(self):
        adapter = self._make_adapter()
        assert adapter.supports_parallel_tool_calls() is True

    def test_count_tokens_estimate(self):
        adapter = self._make_adapter()
        count = adapter.count_tokens([Message(role="user", content="Hello world test")])
        assert count > 0


# =====================================================================
# Adapter Factory Tests (entry.py)
# =====================================================================


class TestAdapterFactory:
    """Test adapter selection in entry.py."""

    def test_default_adapter_is_litellm(self):
        from agent_framework.infra.config import ModelConfig
        cfg = ModelConfig()
        assert cfg.adapter_type == "litellm"

    def test_adapter_type_field_accepts_all_values(self):
        from agent_framework.infra.config import ModelConfig
        for t in ("litellm", "openai", "anthropic", "google"):
            cfg = ModelConfig(adapter_type=t)
            assert cfg.adapter_type == t

    def test_api_key_field(self):
        from agent_framework.infra.config import ModelConfig
        cfg = ModelConfig(api_key="sk-test-123")
        assert cfg.api_key == "sk-test-123"

    def test_api_key_default_none(self):
        from agent_framework.infra.config import ModelConfig
        cfg = ModelConfig()
        assert cfg.api_key is None

    def test_create_openai_adapter(self):
        with patch("openai.AsyncOpenAI"):
            from agent_framework.entry import AgentFramework
            from agent_framework.infra.config import (FrameworkConfig,
                                                      ModelConfig)

            config = FrameworkConfig(model=ModelConfig(adapter_type="openai", default_model_name="gpt-4o"))
            fw = AgentFramework(config=config)
            adapter = fw._create_model_adapter()

            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter
            assert isinstance(adapter, OpenAIAdapter)

    def test_create_anthropic_adapter(self):
        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.entry import AgentFramework
            from agent_framework.infra.config import (FrameworkConfig,
                                                      ModelConfig)

            config = FrameworkConfig(model=ModelConfig(adapter_type="anthropic", default_model_name="claude-sonnet-4-20250514"))
            fw = AgentFramework(config=config)
            adapter = fw._create_model_adapter()

            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter
            assert isinstance(adapter, AnthropicAdapter)

    def test_create_google_adapter(self):
        _mock_google_genai()
        try:
            from agent_framework.entry import AgentFramework
            from agent_framework.infra.config import (FrameworkConfig,
                                                      ModelConfig)

            config = FrameworkConfig(model=ModelConfig(adapter_type="google", default_model_name="gemini-2.5-flash"))
            fw = AgentFramework(config=config)
            adapter = fw._create_model_adapter()

            from agent_framework.adapters.model.google_adapter import \
                GoogleAdapter
            assert isinstance(adapter, GoogleAdapter)
        finally:
            _cleanup_google_genai()

    def test_create_litellm_adapter_default(self):
        from agent_framework.entry import AgentFramework
        from agent_framework.infra.config import FrameworkConfig, ModelConfig

        config = FrameworkConfig(model=ModelConfig(adapter_type="litellm"))
        fw = AgentFramework(config=config)
        adapter = fw._create_model_adapter()

        from agent_framework.adapters.model.litellm_adapter import \
            LiteLLMAdapter
        assert isinstance(adapter, LiteLLMAdapter)

    def test_create_litellm_adapter_unknown_type(self):
        """Unknown adapter_type falls through to litellm."""
        from agent_framework.entry import AgentFramework
        from agent_framework.infra.config import FrameworkConfig, ModelConfig

        config = FrameworkConfig(model=ModelConfig(adapter_type="unknown"))
        fw = AgentFramework(config=config)
        adapter = fw._create_model_adapter()

        from agent_framework.adapters.model.litellm_adapter import \
            LiteLLMAdapter
        assert isinstance(adapter, LiteLLMAdapter)


# =====================================================================
# Cross-adapter consistency tests
# =====================================================================


class TestCrossAdapterConsistency:
    """Verify all adapters implement the same interface correctly."""

    def setup_method(self):
        _mock_google_genai()

    def teardown_method(self):
        _cleanup_google_genai()

    def test_all_adapters_inherit_base(self):
        from agent_framework.adapters.model.base_adapter import \
            BaseModelAdapter

        with patch("openai.AsyncOpenAI"):
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter
            assert issubclass(OpenAIAdapter, BaseModelAdapter)

        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter
            assert issubclass(AnthropicAdapter, BaseModelAdapter)

        from agent_framework.adapters.model.google_adapter import GoogleAdapter
        assert issubclass(GoogleAdapter, BaseModelAdapter)

    def test_all_adapters_support_parallel_tool_calls(self):
        with patch("openai.AsyncOpenAI"):
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter
            assert OpenAIAdapter(model_name="gpt-4o").supports_parallel_tool_calls() is True

        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter
            assert AnthropicAdapter(model_name="claude-sonnet-4-20250514").supports_parallel_tool_calls() is True

        from agent_framework.adapters.model.google_adapter import GoogleAdapter
        assert GoogleAdapter(model_name="gemini-2.5-flash").supports_parallel_tool_calls() is True

    def test_all_adapters_count_tokens_return_int(self):
        msgs = [Message(role="user", content="Test message")]

        with patch("openai.AsyncOpenAI"):
            from agent_framework.adapters.model.openai_adapter import \
                OpenAIAdapter
            assert isinstance(OpenAIAdapter(model_name="gpt-4o").count_tokens(msgs), int)

        with patch("anthropic.AsyncAnthropic"):
            from agent_framework.adapters.model.anthropic_adapter import \
                AnthropicAdapter
            assert isinstance(AnthropicAdapter(model_name="claude-sonnet-4-20250514").count_tokens(msgs), int)

        from agent_framework.adapters.model.google_adapter import GoogleAdapter
        assert isinstance(GoogleAdapter(model_name="gemini-2.5-flash").count_tokens(msgs), int)


# ─── Fallback Adapter Tests ──────────────────────────────────────────



class _StubAdapter(_BaseModelAdapter):
    """Minimal adapter stub for fallback tests."""

    def __init__(
        self,
        *,
        complete_side_effect: Exception | ModelResponse | None = None,
        stream_side_effect: Exception | list[ModelChunk] | None = None,
    ) -> None:
        super().__init__()
        self._complete_side_effect = complete_side_effect
        self._stream_side_effect = stream_side_effect
        self.complete_called = False
        self.stream_called = False

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        self.complete_called = True
        if isinstance(self._complete_side_effect, Exception):
            raise self._complete_side_effect
        return self._complete_side_effect  # type: ignore[return-value]

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ModelChunk]:
        self.stream_called = True
        if isinstance(self._stream_side_effect, Exception):
            raise self._stream_side_effect
        for chunk in (self._stream_side_effect or []):
            yield chunk

    def count_tokens(self, messages: list[Message]) -> int:
        return 42


_OK_RESPONSE = ModelResponse(
    content="hello",
    tool_calls=[],
    finish_reason="stop",
    usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
)

_OK_RESPONSE_2 = ModelResponse(
    content="fallback hello",
    tool_calls=[],
    finish_reason="stop",
    usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
)

_OK_RESPONSE_3 = ModelResponse(
    content="fallback-2 hello",
    tool_calls=[],
    finish_reason="stop",
    usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
)


class TestFallbackModelAdapter:
    """Tests for the FallbackModelAdapter wrapper."""

    @pytest.mark.asyncio
    async def test_primary_succeeds_fallback_not_called(self) -> None:
        primary = _StubAdapter(complete_side_effect=_OK_RESPONSE)
        fallback = _StubAdapter(complete_side_effect=_OK_RESPONSE_2)
        adapter = FallbackModelAdapter(primary=primary, fallbacks=[fallback])

        result = await adapter.complete(SAMPLE_MESSAGES)

        assert result.content == "hello"
        assert primary.complete_called
        assert not fallback.complete_called

    @pytest.mark.asyncio
    async def test_primary_fails_fallback1_succeeds(self) -> None:
        primary = _StubAdapter(complete_side_effect=LLMCallError("primary down"))
        fallback = _StubAdapter(complete_side_effect=_OK_RESPONSE_2)
        adapter = FallbackModelAdapter(primary=primary, fallbacks=[fallback])

        result = await adapter.complete(SAMPLE_MESSAGES)

        assert result.content == "fallback hello"
        assert primary.complete_called
        assert fallback.complete_called

    @pytest.mark.asyncio
    async def test_primary_fails_fallback1_fails_fallback2_succeeds(self) -> None:
        primary = _StubAdapter(complete_side_effect=LLMRateLimitError("rate limited"))
        fb1 = _StubAdapter(complete_side_effect=LLMTimeoutError("timeout"))
        fb2 = _StubAdapter(complete_side_effect=_OK_RESPONSE_3)
        adapter = FallbackModelAdapter(primary=primary, fallbacks=[fb1, fb2])

        result = await adapter.complete(SAMPLE_MESSAGES)

        assert result.content == "fallback-2 hello"
        assert primary.complete_called
        assert fb1.complete_called
        assert fb2.complete_called

    @pytest.mark.asyncio
    async def test_auth_error_not_retried_on_fallbacks(self) -> None:
        primary = _StubAdapter(complete_side_effect=LLMAuthError("bad key"))
        fallback = _StubAdapter(complete_side_effect=_OK_RESPONSE_2)
        adapter = FallbackModelAdapter(primary=primary, fallbacks=[fallback])

        with pytest.raises(LLMAuthError, match="bad key"):
            await adapter.complete(SAMPLE_MESSAGES)

        assert primary.complete_called
        assert not fallback.complete_called

    @pytest.mark.asyncio
    async def test_all_fail_raises_last_error(self) -> None:
        primary = _StubAdapter(complete_side_effect=LLMCallError("primary fail"))
        fb1 = _StubAdapter(complete_side_effect=LLMRateLimitError("fb1 rate limit"))
        fb2 = _StubAdapter(complete_side_effect=LLMTimeoutError("fb2 timeout"))
        adapter = FallbackModelAdapter(primary=primary, fallbacks=[fb1, fb2])

        with pytest.raises(LLMTimeoutError, match="fb2 timeout"):
            await adapter.complete(SAMPLE_MESSAGES)

    @pytest.mark.asyncio
    async def test_stream_primary_succeeds(self) -> None:
        chunks = [ModelChunk(delta_content="hi"), ModelChunk(finish_reason="stop")]
        primary = _StubAdapter(stream_side_effect=chunks)
        fallback = _StubAdapter(stream_side_effect=[])
        adapter = FallbackModelAdapter(primary=primary, fallbacks=[fallback])

        collected = []
        async for chunk in adapter.stream_complete(SAMPLE_MESSAGES):
            collected.append(chunk)

        assert len(collected) == 2
        assert collected[0].delta_content == "hi"
        assert not fallback.stream_called

    @pytest.mark.asyncio
    async def test_stream_primary_fails_fallback_succeeds(self) -> None:
        fb_chunks = [ModelChunk(delta_content="fallback"), ModelChunk(finish_reason="stop")]
        primary = _StubAdapter(stream_side_effect=LLMCallError("stream fail"))
        fallback = _StubAdapter(stream_side_effect=fb_chunks)
        adapter = FallbackModelAdapter(primary=primary, fallbacks=[fallback])

        collected = []
        async for chunk in adapter.stream_complete(SAMPLE_MESSAGES):
            collected.append(chunk)

        assert len(collected) == 2
        assert collected[0].delta_content == "fallback"

    @pytest.mark.asyncio
    async def test_stream_auth_error_not_retried(self) -> None:
        primary = _StubAdapter(stream_side_effect=LLMAuthError("bad key"))
        fallback = _StubAdapter(stream_side_effect=[ModelChunk(delta_content="ok")])
        adapter = FallbackModelAdapter(primary=primary, fallbacks=[fallback])

        with pytest.raises(LLMAuthError, match="bad key"):
            async for _ in adapter.stream_complete(SAMPLE_MESSAGES):
                pass

        assert not fallback.stream_called

    def test_count_tokens_delegates_to_primary(self) -> None:
        primary = _StubAdapter()
        fallback = _StubAdapter()
        adapter = FallbackModelAdapter(primary=primary, fallbacks=[fallback])

        assert adapter.count_tokens(SAMPLE_MESSAGES) == 42

    def test_session_propagated_to_all_adapters(self) -> None:
        primary = _StubAdapter()
        fb1 = _StubAdapter()
        fb2 = _StubAdapter()
        adapter = FallbackModelAdapter(primary=primary, fallbacks=[fb1, fb2])

        adapter.begin_session("test-session")
        assert primary._session.active
        assert fb1._session.active
        assert fb2._session.active

        adapter.end_session()
        assert not primary._session.active
        assert not fb1._session.active
        assert not fb2._session.active
