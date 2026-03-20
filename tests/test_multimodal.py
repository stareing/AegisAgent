"""Tests for multimodal message support.

Covers:
1. ContentPart model
2. Message with content_parts
3. Message.has_multimodal / text_content properties
4. Backward compatibility (text-only messages unchanged)
5. Adapter conversion (OpenAI, Anthropic, Google formats)
6. Chain-level: coordinator input, compression protection, text_content usage
7. Token estimation with multimodal content
"""

from __future__ import annotations

import pytest

from agent_framework.models.message import ContentPart, Message

# ---------------------------------------------------------------------------
# ContentPart model tests
# ---------------------------------------------------------------------------

class TestContentPart:
    def test_text_part(self) -> None:
        p = ContentPart(type="text", text="hello")
        assert p.type == "text"
        assert p.text == "hello"

    def test_image_url_part(self) -> None:
        p = ContentPart(
            type="image_url",
            image_url="https://example.com/image.jpg",
            detail="high",
        )
        assert p.type == "image_url"
        assert p.image_url == "https://example.com/image.jpg"
        assert p.detail == "high"

    def test_image_base64_part(self) -> None:
        p = ContentPart(
            type="image_base64",
            media_type="image/png",
            data="iVBORw0KGgo=",
        )
        assert p.type == "image_base64"
        assert p.media_type == "image/png"
        assert p.data == "iVBORw0KGgo="

    def test_audio_part(self) -> None:
        p = ContentPart(
            type="audio",
            media_type="audio/wav",
            data="UklGR...",
        )
        assert p.type == "audio"
        assert p.media_type == "audio/wav"

    def test_file_part(self) -> None:
        p = ContentPart(
            type="file",
            file_uri="gs://bucket/file.pdf",
            media_type="application/pdf",
        )
        assert p.type == "file"
        assert p.file_uri == "gs://bucket/file.pdf"

    def test_default_type_is_text(self) -> None:
        p = ContentPart(text="hello")
        assert p.type == "text"


# ---------------------------------------------------------------------------
# Message multimodal tests
# ---------------------------------------------------------------------------

class TestMessageMultimodal:
    def test_text_only_backward_compat(self) -> None:
        """Text-only messages work exactly as before."""
        m = Message(role="user", content="hello")
        assert m.content == "hello"
        assert m.content_parts is None
        assert m.has_multimodal is False
        assert m.text_content == "hello"

    def test_content_parts_text_only(self) -> None:
        m = Message(
            role="user",
            content_parts=[ContentPart(type="text", text="hello")],
        )
        assert m.has_multimodal is False
        assert m.text_content == "hello"

    def test_content_parts_with_image(self) -> None:
        m = Message(
            role="user",
            content_parts=[
                ContentPart(type="text", text="What's in this image?"),
                ContentPart(type="image_url", image_url="https://example.com/img.jpg"),
            ],
        )
        assert m.has_multimodal is True
        assert m.text_content == "What's in this image?"

    def test_content_parts_precedence(self) -> None:
        """content_parts should take precedence over content in text_content."""
        m = Message(
            role="user",
            content="old text",
            content_parts=[ContentPart(type="text", text="new text")],
        )
        assert m.text_content == "new text"

    def test_text_content_with_no_text_parts(self) -> None:
        """If content_parts has only images, fall back to content."""
        m = Message(
            role="user",
            content="describe this",
            content_parts=[
                ContentPart(type="image_url", image_url="https://example.com/img.jpg"),
            ],
        )
        assert m.text_content == "describe this"

    def test_multiple_text_parts_joined(self) -> None:
        m = Message(
            role="user",
            content_parts=[
                ContentPart(type="text", text="Part 1"),
                ContentPart(type="text", text="Part 2"),
            ],
        )
        assert m.text_content == "Part 1\nPart 2"

    def test_system_message_not_multimodal(self) -> None:
        """System messages should remain text-only."""
        m = Message(role="system", content="You are a helpful assistant.")
        assert m.has_multimodal is False


# ---------------------------------------------------------------------------
# OpenAI format conversion tests
# ---------------------------------------------------------------------------

class TestOpenAIConversion:
    def _convert(self, messages: list[Message]) -> list[dict]:
        from agent_framework.adapters.model.openai_adapter import OpenAIAdapter
        return OpenAIAdapter._messages_to_dicts(messages)

    def test_text_only_unchanged(self) -> None:
        result = self._convert([Message(role="user", content="hello")])
        assert result[0]["content"] == "hello"
        assert isinstance(result[0]["content"], str)

    def test_image_url(self) -> None:
        m = Message(
            role="user",
            content_parts=[
                ContentPart(type="text", text="What is this?"),
                ContentPart(type="image_url", image_url="https://img.com/a.jpg", detail="high"),
            ],
        )
        result = self._convert([m])
        content = result[0]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "What is this?"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"] == "https://img.com/a.jpg"
        assert content[1]["image_url"]["detail"] == "high"

    def test_image_base64_as_data_uri(self) -> None:
        m = Message(
            role="user",
            content_parts=[
                ContentPart(type="image_base64", media_type="image/png", data="abc123"),
            ],
        )
        result = self._convert([m])
        content = result[0]["content"]
        assert content[0]["type"] == "image_url"
        assert content[0]["image_url"]["url"] == "data:image/png;base64,abc123"

    def test_audio_part(self) -> None:
        m = Message(
            role="user",
            content_parts=[
                ContentPart(type="audio", media_type="audio/mp3", data="audiodata"),
            ],
        )
        result = self._convert([m])
        content = result[0]["content"]
        assert content[0]["type"] == "input_audio"
        assert content[0]["input_audio"]["format"] == "mp3"


# ---------------------------------------------------------------------------
# Anthropic format conversion tests
# ---------------------------------------------------------------------------

class TestAnthropicConversion:
    def _convert(self, messages: list[Message]) -> list[dict]:
        from agent_framework.adapters.model.anthropic_adapter import \
            AnthropicAdapter
        return AnthropicAdapter._convert_messages(messages)

    def test_text_only_unchanged(self) -> None:
        result = self._convert([Message(role="user", content="hello")])
        assert result[0]["content"] == "hello"

    def test_image_base64(self) -> None:
        m = Message(
            role="user",
            content_parts=[
                ContentPart(type="text", text="Describe this"),
                ContentPart(type="image_base64", media_type="image/jpeg", data="jpg_data"),
            ],
        )
        result = self._convert([m])
        content = result[0]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "Describe this"}
        assert content[1]["type"] == "image"
        assert content[1]["source"]["type"] == "base64"
        assert content[1]["source"]["media_type"] == "image/jpeg"
        assert content[1]["source"]["data"] == "jpg_data"

    def test_image_url(self) -> None:
        m = Message(
            role="user",
            content_parts=[
                ContentPart(type="image_url", image_url="https://img.com/a.jpg"),
            ],
        )
        result = self._convert([m])
        content = result[0]["content"]
        assert content[0]["type"] == "image"
        assert content[0]["source"]["type"] == "url"


# ---------------------------------------------------------------------------
# Google format conversion tests
# ---------------------------------------------------------------------------

class TestGoogleConversion:
    def _convert(self, messages: list[Message]) -> tuple:
        from agent_framework.adapters.model.google_adapter import GoogleAdapter
        return GoogleAdapter._convert_messages(messages)

    def test_text_only_unchanged(self) -> None:
        _, contents = self._convert([Message(role="user", content="hello")])
        assert contents[0]["parts"][0] == {"text": "hello"}

    def test_image_base64(self) -> None:
        m = Message(
            role="user",
            content_parts=[
                ContentPart(type="text", text="What is this?"),
                ContentPart(type="image_base64", media_type="image/png", data="pngdata"),
            ],
        )
        _, contents = self._convert([m])
        parts = contents[0]["parts"]
        assert parts[0] == {"text": "What is this?"}
        assert parts[1]["inline_data"]["mime_type"] == "image/png"
        assert parts[1]["inline_data"]["data"] == "pngdata"

    def test_audio_inline(self) -> None:
        m = Message(
            role="user",
            content_parts=[
                ContentPart(type="audio", media_type="audio/wav", data="wavdata"),
            ],
        )
        _, contents = self._convert([m])
        parts = contents[0]["parts"]
        assert parts[0]["inline_data"]["mime_type"] == "audio/wav"

    def test_file_reference(self) -> None:
        m = Message(
            role="user",
            content_parts=[
                ContentPart(type="file", file_uri="gs://bucket/doc.pdf", media_type="application/pdf"),
            ],
        )
        _, contents = self._convert([m])
        parts = contents[0]["parts"]
        assert parts[0]["file_data"]["file_uri"] == "gs://bucket/doc.pdf"


# ---------------------------------------------------------------------------
# Chain-level: Coordinator _build_user_message
# ---------------------------------------------------------------------------

class TestCoordinatorMultimodal:
    def test_build_user_message_text_only(self) -> None:
        from agent_framework.agent.coordinator import RunCoordinator
        coord = RunCoordinator()
        msg = coord._build_user_message("hello")
        assert msg.role == "user"
        assert msg.content == "hello"
        assert msg.content_parts is None

    def test_build_user_message_with_content_parts(self) -> None:
        from agent_framework.agent.coordinator import RunCoordinator
        coord = RunCoordinator()
        parts = [
            ContentPart(type="text", text="Describe this image"),
            ContentPart(type="image_url", image_url="https://example.com/img.jpg"),
        ]
        msg = coord._build_user_message("Describe this image", parts)
        assert msg.role == "user"
        assert msg.content == "Describe this image"
        assert msg.content_parts is not None
        assert len(msg.content_parts) == 2
        assert msg.has_multimodal is True
        assert msg.text_content == "Describe this image"


# ---------------------------------------------------------------------------
# Chain-level: Compression protects multimodal messages
# ---------------------------------------------------------------------------

class TestCompressionMultimodal:
    def test_multimodal_group_marked_protected(self) -> None:
        """Groups containing multimodal messages should be marked protected."""
        from agent_framework.context.transaction_group import \
            ToolTransactionGroup

        multimodal_msg = Message(
            role="user",
            content="What is this?",
            content_parts=[
                ContentPart(type="text", text="What is this?"),
                ContentPart(type="image_url", image_url="https://example.com/img.jpg"),
            ],
        )
        text_msg = Message(role="assistant", content="It is a cat.")
        groups = [
            ToolTransactionGroup(
                group_id="g1",
                messages=[multimodal_msg, text_msg],
                token_estimate=100,
            ),
            ToolTransactionGroup(
                group_id="g2",
                messages=[Message(role="user", content="Thanks")],
                token_estimate=10,
            ),
            ToolTransactionGroup(
                group_id="g3",
                messages=[Message(role="user", content="More")],
                token_estimate=10,
            ),
        ]

        # Simulate the protection logic from _llm_summarize
        for g in groups:
            if any(msg.has_multimodal for msg in g.messages):
                g.protected = True
        assert groups[0].protected is True
        assert groups[1].protected is False
        assert groups[2].protected is False

    def test_multimodal_message_survives_trim(self) -> None:
        """Protected multimodal groups should not be trimmed by ContextBuilder."""
        from agent_framework.context.builder import ContextBuilder
        from agent_framework.context.transaction_group import \
            ToolTransactionGroup

        builder = ContextBuilder(max_context_tokens=8192)
        multimodal_msg = Message(
            role="user",
            content="Describe",
            content_parts=[
                ContentPart(type="text", text="Describe"),
                ContentPart(type="image_base64", media_type="image/png", data="abc"),
            ],
        )
        groups = [
            ToolTransactionGroup(
                group_id="g1",
                messages=[multimodal_msg],
                token_estimate=50,
                protected=True,  # Pre-marked by compressor
            ),
            ToolTransactionGroup(
                group_id="g2",
                messages=[Message(role="user", content="x" * 400)],
                token_estimate=100,
            ),
        ]
        trimmed = builder._trim_session_groups(groups, token_limit=80)
        # Protected group must survive even if over budget
        assert any(g.group_id == "g1" for g in trimmed)


# ---------------------------------------------------------------------------
# Chain-level: messages_to_text uses text_content
# ---------------------------------------------------------------------------

class TestSummarizerMultimodal:
    def test_messages_to_text_uses_text_content(self) -> None:
        """messages_to_text should use text_content, not raw .content."""
        from agent_framework.context.summarizer import messages_to_text
        m = Message(
            role="user",
            content="fallback",
            content_parts=[ContentPart(type="text", text="primary text")],
        )
        text = messages_to_text([m])
        assert "primary text" in text
        assert "fallback" not in text

    def test_messages_to_text_annotates_multimodal(self) -> None:
        """Multimodal messages should be annotated with media types."""
        from agent_framework.context.summarizer import messages_to_text
        m = Message(
            role="user",
            content_parts=[
                ContentPart(type="text", text="Look at this"),
                ContentPart(type="image_url", image_url="https://example.com/img.jpg"),
            ],
        )
        text = messages_to_text([m])
        assert "[+image_url]" in text
        assert "Look at this" in text

    def test_messages_to_text_text_only_no_annotation(self) -> None:
        """Text-only messages should not have media annotations."""
        from agent_framework.context.summarizer import messages_to_text
        m = Message(role="user", content="hello")
        text = messages_to_text([m])
        assert "[+" not in text
        assert "hello" in text

    def test_is_summary_message_uses_text_content(self) -> None:
        from agent_framework.context.summarizer import (is_summary_message,
                                                        wrap_summary)
        summary_text = wrap_summary("test summary")
        m = Message(
            role="user",
            content_parts=[ContentPart(type="text", text=summary_text)],
        )
        assert is_summary_message(m) is True

    def test_has_multimodal_content_helper(self) -> None:
        from agent_framework.context.summarizer import has_multimodal_content
        m1 = Message(role="user", content="text only")
        assert has_multimodal_content(m1) is False

        m2 = Message(
            role="user",
            content_parts=[
                ContentPart(type="text", text="hello"),
                ContentPart(type="image_url", image_url="https://example.com/img.jpg"),
            ],
        )
        assert has_multimodal_content(m2) is True


# ---------------------------------------------------------------------------
# Chain-level: Token estimation with multimodal
# ---------------------------------------------------------------------------

class TestTokenEstimation:
    def test_builder_rough_count_multimodal(self) -> None:
        """Builder._rough_count should account for multimodal content."""
        from agent_framework.context.builder import ContextBuilder
        text_msg = Message(role="user", content="hello")
        multimodal_msg = Message(
            role="user",
            content_parts=[
                ContentPart(type="text", text="hello"),
                ContentPart(type="image_url", image_url="https://example.com/img.jpg"),
            ],
        )
        text_tokens = ContextBuilder._rough_count([text_msg])
        multi_tokens = ContextBuilder._rough_count([multimodal_msg])
        # Multimodal should estimate more tokens than text-only
        assert multi_tokens > text_tokens

    def test_compressor_rough_count_multimodal(self) -> None:
        """Compressor._rough_count should account for multimodal content."""
        from agent_framework.context.compressor import ContextCompressor
        text_msg = Message(role="user", content="hello")
        multimodal_msg = Message(
            role="user",
            content_parts=[
                ContentPart(type="text", text="hello"),
                ContentPart(type="image_base64", media_type="image/png", data="x" * 400),
            ],
        )
        text_tokens = ContextCompressor._rough_count([text_msg])
        multi_tokens = ContextCompressor._rough_count([multimodal_msg])
        assert multi_tokens > text_tokens

    def test_compressor_cache_key_uses_text_content(self) -> None:
        """Cache key should use text_content for consistent hashing."""
        from agent_framework.context.compressor import ContextCompressor
        from agent_framework.context.transaction_group import \
            ToolTransactionGroup
        m1 = Message(
            role="user",
            content="fallback",
            content_parts=[ContentPart(type="text", text="primary")],
        )
        m2 = Message(role="user", content="primary")
        g1 = ToolTransactionGroup(messages=[m1])
        g2 = ToolTransactionGroup(messages=[m2])
        # Both should produce the same cache key since text_content is "primary"
        key1 = ContextCompressor._compute_cache_key([g1])
        key2 = ContextCompressor._compute_cache_key([g2])
        assert key1 == key2


# ---------------------------------------------------------------------------
# Chain-level: ContextBuilder preserves multimodal in spawn seed
# ---------------------------------------------------------------------------

class TestSpawnSeedMultimodal:
    def test_filtered_spawn_seed_preserves_content_parts(self) -> None:
        """build_filtered_spawn_seed should preserve content_parts on messages."""
        from agent_framework.context.builder import ContextBuilder
        builder = ContextBuilder()
        multimodal_msg = Message(
            role="user",
            content="Describe this",
            content_parts=[
                ContentPart(type="text", text="Describe this"),
                ContentPart(type="image_url", image_url="https://example.com/img.jpg"),
            ],
        )
        session = [multimodal_msg, Message(role="assistant", content="It's a cat.")]
        seed = builder.build_filtered_spawn_seed(session, "new task", token_budget=4096)
        # The multimodal message should be in the seed with content_parts intact
        multimodal_in_seed = [m for m in seed if m.content_parts]
        assert len(multimodal_in_seed) == 1
        assert multimodal_in_seed[0].has_multimodal is True

    def test_spawn_seed_preserves_content_parts(self) -> None:
        """build_spawn_seed should preserve content_parts on messages."""
        from agent_framework.context.builder import ContextBuilder
        builder = ContextBuilder()
        multimodal_msg = Message(
            role="user",
            content="Describe this",
            content_parts=[
                ContentPart(type="text", text="Describe this"),
                ContentPart(type="image_base64", media_type="image/png", data="data"),
            ],
        )
        session = [multimodal_msg]
        seed = builder.build_spawn_seed(session, "new task", token_budget=4096)
        multimodal_in_seed = [m for m in seed if m.content_parts]
        assert len(multimodal_in_seed) == 1


# ---------------------------------------------------------------------------
# Chain-level: Memory extraction with multimodal input
# ---------------------------------------------------------------------------

class TestMemoryMultimodal:
    def test_extract_candidates_text_only_input(self) -> None:
        """Memory extraction works with text from multimodal message."""
        from unittest.mock import MagicMock

        from agent_framework.memory.default_manager import DefaultMemoryManager
        store = MagicMock()
        mgr = DefaultMemoryManager(store)
        # Memory extraction receives text_content, not raw multimodal
        # The coordinator passes task (str) to record_turn
        candidates = mgr.extract_candidates("记住我喜欢Python", None, [])
        assert len(candidates) >= 1
        assert candidates[0].content == "记住我喜欢Python"
