"""Strict unit tests for OpenAI-compatible model adapters.

Covers:
- OpenAICompatibleAdapter base (init, build_kwargs, count_tokens, provider defaults)
- DeepSeek, Doubao, Qwen, Zhipu, MiniMax provider subclasses
- CustomAdapter
- Entry factory wiring
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_framework.models.message import Message, ModelResponse, TokenUsage, ToolCallRequest


# =====================================================================
# Helpers: mock openai module
# =====================================================================

_original_openai = sys.modules.get("openai")


def _mock_openai_module():
    """Inject a mock openai module into sys.modules for testing."""
    mock_mod = ModuleType("openai")

    class MockAsyncOpenAI:
        def __init__(self, **kwargs):
            self._kwargs = kwargs
            self.chat = MagicMock()
            self.chat.completions = MagicMock()
            self.chat.completions.create = AsyncMock()

    mock_mod.AsyncOpenAI = MockAsyncOpenAI
    mock_mod.RateLimitError = type("RateLimitError", (Exception,), {})
    mock_mod.AuthenticationError = type("AuthenticationError", (Exception,), {})
    mock_mod.APITimeoutError = type("APITimeoutError", (Exception,), {})
    mock_mod.APIError = type("APIError", (Exception,), {})

    sys.modules["openai"] = mock_mod
    return mock_mod


def _cleanup_openai():
    if _original_openai is not None:
        sys.modules["openai"] = _original_openai
    else:
        sys.modules.pop("openai", None)


# =====================================================================
# Provider defaults
# =====================================================================


class TestProviderDefaults:
    def setup_method(self):
        self._mock = _mock_openai_module()

    def teardown_method(self):
        _cleanup_openai()

    def test_deepseek_defaults(self):
        from agent_framework.adapters.model.openai_compatible_adapter import DeepSeekAdapter
        adapter = DeepSeekAdapter(api_key="sk-test")
        assert adapter.model_name == "deepseek-chat"
        assert adapter._provider == "deepseek"

    def test_doubao_defaults(self):
        from agent_framework.adapters.model.openai_compatible_adapter import DoubaoAdapter
        adapter = DoubaoAdapter(api_key="sk-test")
        assert adapter.model_name == "doubao-pro-32k"
        assert adapter._provider == "doubao"

    def test_qwen_defaults(self):
        from agent_framework.adapters.model.openai_compatible_adapter import QwenAdapter
        adapter = QwenAdapter(api_key="sk-test")
        assert adapter.model_name == "qwen-plus"
        assert adapter._provider == "qwen"

    def test_zhipu_defaults(self):
        from agent_framework.adapters.model.openai_compatible_adapter import ZhipuAdapter
        adapter = ZhipuAdapter(api_key="sk-test")
        assert adapter.model_name == "glm-4"
        assert adapter._provider == "zhipu"

    def test_minimax_defaults(self):
        from agent_framework.adapters.model.openai_compatible_adapter import MiniMaxAdapter
        adapter = MiniMaxAdapter(api_key="sk-test")
        assert adapter.model_name == "abab6.5s-chat"
        assert adapter._provider == "minimax"

    def test_custom_model_override(self):
        from agent_framework.adapters.model.openai_compatible_adapter import DeepSeekAdapter
        adapter = DeepSeekAdapter(model_name="deepseek-coder", api_key="sk-test")
        assert adapter.model_name == "deepseek-coder"

    def test_custom_adapter_requires_api_base(self):
        from agent_framework.adapters.model.openai_compatible_adapter import CustomAdapter
        with pytest.raises(ValueError, match="api_base is required"):
            CustomAdapter(api_key="sk-test")

    def test_custom_adapter_with_api_base(self):
        from agent_framework.adapters.model.openai_compatible_adapter import CustomAdapter
        adapter = CustomAdapter(
            api_key="sk-test",
            api_base="https://my-llm.example.com/v1",
            model_name="my-model",
        )
        assert adapter.model_name == "my-model"

    def test_api_base_override(self):
        from agent_framework.adapters.model.openai_compatible_adapter import DeepSeekAdapter
        adapter = DeepSeekAdapter(
            api_key="sk-test",
            api_base="https://custom-deepseek.example.com",
        )
        # Should use custom base instead of default
        assert adapter._client._kwargs["base_url"] == "https://custom-deepseek.example.com"

    def test_extra_headers(self):
        from agent_framework.adapters.model.openai_compatible_adapter import DoubaoAdapter
        adapter = DoubaoAdapter(
            api_key="sk-test",
            extra_headers={"X-Custom": "value"},
        )
        assert adapter._extra_headers == {"X-Custom": "value"}
        assert adapter._client._kwargs["default_headers"] == {"X-Custom": "value"}


# =====================================================================
# Message conversion & kwargs building
# =====================================================================


class TestBuildKwargs:
    def setup_method(self):
        self._mock = _mock_openai_module()

    def teardown_method(self):
        _cleanup_openai()

    def test_basic_kwargs(self):
        from agent_framework.adapters.model.openai_compatible_adapter import DeepSeekAdapter
        adapter = DeepSeekAdapter(api_key="sk-test", temperature=0.5)
        messages = [Message(role="user", content="hello")]
        kwargs = adapter._build_kwargs(messages)
        assert kwargs["model"] == "deepseek-chat"
        assert kwargs["temperature"] == 0.5
        assert kwargs["stream"] is False
        assert len(kwargs["messages"]) == 1
        assert kwargs["messages"][0]["role"] == "user"
        assert kwargs["messages"][0]["content"] == "hello"

    def test_kwargs_with_tools(self):
        from agent_framework.adapters.model.openai_compatible_adapter import QwenAdapter
        adapter = QwenAdapter(api_key="sk-test")
        tools = [{"type": "function", "function": {"name": "calc", "parameters": {}}}]
        kwargs = adapter._build_kwargs(
            [Message(role="user", content="calc")], tools=tools
        )
        assert "tools" in kwargs
        assert kwargs["tools"] == tools

    def test_kwargs_max_tokens(self):
        from agent_framework.adapters.model.openai_compatible_adapter import ZhipuAdapter
        adapter = ZhipuAdapter(api_key="sk-test", max_output_tokens=2048)
        kwargs = adapter._build_kwargs(
            [Message(role="user", content="hi")], max_tokens=1024
        )
        assert kwargs["max_tokens"] == 1024  # explicit overrides default

    def test_kwargs_stream(self):
        from agent_framework.adapters.model.openai_compatible_adapter import MiniMaxAdapter
        adapter = MiniMaxAdapter(api_key="sk-test")
        kwargs = adapter._build_kwargs(
            [Message(role="user", content="hi")], stream=True
        )
        assert kwargs["stream"] is True


# =====================================================================
# Token counting
# =====================================================================


class TestTokenCounting:
    def setup_method(self):
        self._mock = _mock_openai_module()

    def teardown_method(self):
        _cleanup_openai()

    def test_ascii_text(self):
        from agent_framework.adapters.model.openai_compatible_adapter import DeepSeekAdapter
        adapter = DeepSeekAdapter(api_key="sk-test")
        msgs = [Message(role="user", content="Hello world, this is a test")]
        count = adapter.count_tokens(msgs)
        assert count > 0

    def test_chinese_text(self):
        from agent_framework.adapters.model.openai_compatible_adapter import QwenAdapter
        adapter = QwenAdapter(api_key="sk-test")
        msgs = [Message(role="user", content="你好世界，这是一个测试")]
        count = adapter.count_tokens(msgs)
        assert count > 0

    def test_mixed_text(self):
        from agent_framework.adapters.model.openai_compatible_adapter import ZhipuAdapter
        adapter = ZhipuAdapter(api_key="sk-test")
        msgs = [Message(role="user", content="Hello 你好 World 世界")]
        count = adapter.count_tokens(msgs)
        assert count > 0

    def test_empty_messages(self):
        from agent_framework.adapters.model.openai_compatible_adapter import DeepSeekAdapter
        adapter = DeepSeekAdapter(api_key="sk-test")
        msgs = [Message(role="user")]
        count = adapter.count_tokens(msgs)
        assert count >= 1

    def test_with_tool_calls(self):
        from agent_framework.adapters.model.openai_compatible_adapter import DeepSeekAdapter
        adapter = DeepSeekAdapter(api_key="sk-test")
        msgs = [Message(
            role="assistant",
            tool_calls=[ToolCallRequest(id="tc1", function_name="search", arguments={"q": "test"})],
        )]
        count = adapter.count_tokens(msgs)
        assert count > 4  # more than just overhead


# =====================================================================
# Complete / retry
# =====================================================================


class TestCompleteAndRetry:
    def setup_method(self):
        self._mock = _mock_openai_module()

    def teardown_method(self):
        _cleanup_openai()

    @pytest.mark.asyncio
    async def test_complete_success(self):
        from agent_framework.adapters.model.openai_compatible_adapter import DeepSeekAdapter
        adapter = DeepSeekAdapter(api_key="sk-test")

        # Mock response
        mock_choice = MagicMock()
        mock_choice.message.content = "Hello!"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_response.model = "deepseek-chat"
        mock_response.id = "resp-123"

        adapter._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await adapter.complete([Message(role="user", content="hi")])
        assert isinstance(result, ModelResponse)
        assert result.content == "Hello!"
        assert result.finish_reason == "stop"
        assert result.usage.total_tokens == 15

    @pytest.mark.asyncio
    async def test_complete_with_tool_calls(self):
        from agent_framework.adapters.model.openai_compatible_adapter import QwenAdapter
        adapter = QwenAdapter(api_key="sk-test")

        mock_tc = MagicMock()
        mock_tc.id = "call_1"
        mock_tc.function.name = "search"
        mock_tc.function.arguments = '{"q": "test"}'

        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.tool_calls = [mock_tc]
        mock_choice.finish_reason = "tool_calls"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 20
        mock_response.usage.completion_tokens = 10
        mock_response.usage.total_tokens = 30
        mock_response.model = "qwen-plus"
        mock_response.id = "resp-456"

        adapter._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await adapter.complete([Message(role="user", content="search something")])
        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].function_name == "search"

    @pytest.mark.asyncio
    async def test_auth_error_not_retried(self):
        from agent_framework.adapters.model.openai_compatible_adapter import ZhipuAdapter
        from agent_framework.adapters.model.base_adapter import LLMAuthError
        adapter = ZhipuAdapter(api_key="bad-key", max_retries=3)

        adapter._client.chat.completions.create = AsyncMock(
            side_effect=adapter._openai.AuthenticationError("invalid key")
        )

        with pytest.raises(LLMAuthError):
            await adapter.complete([Message(role="user", content="hi")])

        # Should have been called only once (no retry on auth errors)
        assert adapter._client.chat.completions.create.call_count == 1

    @pytest.mark.asyncio
    async def test_rate_limit_retried(self):
        from agent_framework.adapters.model.openai_compatible_adapter import MiniMaxAdapter
        from agent_framework.adapters.model.base_adapter import LLMRateLimitError
        adapter = MiniMaxAdapter(api_key="sk-test", max_retries=2)

        adapter._client.chat.completions.create = AsyncMock(
            side_effect=adapter._openai.RateLimitError("too fast")
        )

        with pytest.raises(LLMRateLimitError):
            await adapter.complete([Message(role="user", content="hi")])

        assert adapter._client.chat.completions.create.call_count == 2

    def test_supports_parallel_tool_calls(self):
        from agent_framework.adapters.model.openai_compatible_adapter import DeepSeekAdapter
        adapter = DeepSeekAdapter(api_key="sk-test")
        assert adapter.supports_parallel_tool_calls() is True


# =====================================================================
# Entry factory integration
# =====================================================================


class TestEntryFactory:
    def setup_method(self):
        self._mock = _mock_openai_module()

    def teardown_method(self):
        _cleanup_openai()

    def _create_adapter(self, adapter_type: str, **overrides):
        from agent_framework.entry import AgentFramework
        from agent_framework.infra.config import FrameworkConfig, ModelConfig

        model_cfg = ModelConfig(
            adapter_type=adapter_type,
            api_key="sk-test",
            **overrides,
        )
        config = FrameworkConfig(model=model_cfg)
        fw = AgentFramework(config=config)
        return fw._create_model_adapter()

    def test_factory_deepseek(self):
        from agent_framework.adapters.model.openai_compatible_adapter import DeepSeekAdapter
        adapter = self._create_adapter("deepseek")
        assert isinstance(adapter, DeepSeekAdapter)

    def test_factory_doubao(self):
        from agent_framework.adapters.model.openai_compatible_adapter import DoubaoAdapter
        adapter = self._create_adapter("doubao")
        assert isinstance(adapter, DoubaoAdapter)

    def test_factory_qwen(self):
        from agent_framework.adapters.model.openai_compatible_adapter import QwenAdapter
        adapter = self._create_adapter("qwen")
        assert isinstance(adapter, QwenAdapter)

    def test_factory_zhipu(self):
        from agent_framework.adapters.model.openai_compatible_adapter import ZhipuAdapter
        adapter = self._create_adapter("zhipu")
        assert isinstance(adapter, ZhipuAdapter)

    def test_factory_minimax(self):
        from agent_framework.adapters.model.openai_compatible_adapter import MiniMaxAdapter
        adapter = self._create_adapter("minimax")
        assert isinstance(adapter, MiniMaxAdapter)

    def test_factory_custom(self):
        from agent_framework.adapters.model.openai_compatible_adapter import CustomAdapter
        adapter = self._create_adapter("custom", api_base="https://my-llm.example.com/v1")
        assert isinstance(adapter, CustomAdapter)
