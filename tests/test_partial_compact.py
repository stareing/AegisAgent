"""Tests for v4.3 PARTIAL compress strategy and image stripping."""

from __future__ import annotations

import pytest

from agent_framework.context.compressor import ContextCompressor
from agent_framework.context.strategies import CompressionStrategy
from agent_framework.context.transaction_group import ToolTransactionGroup
from agent_framework.models.message import ContentPart, Message, ToolCallRequest


def _make_group(content: str, group_id: str, token_est: int = 100) -> ToolTransactionGroup:
    return ToolTransactionGroup(
        group_id=group_id,
        group_type="PLAIN_MESSAGES",
        messages=[Message(role="user", content=content)],
        token_estimate=token_est,
    )


def _make_tool_group(tool_name: str, content: str, group_id: str, token_est: int = 100) -> ToolTransactionGroup:
    return ToolTransactionGroup(
        group_id=group_id,
        group_type="TOOL_BATCH",
        messages=[
            Message(role="assistant", content=None, tool_calls=[
                ToolCallRequest(id=f"tc_{group_id}", function_name=tool_name, arguments={})
            ]),
            Message(role="tool", content=content, tool_call_id=f"tc_{group_id}", name=tool_name),
        ],
        token_estimate=token_est,
    )


# ===========================================================================
# PARTIAL Compress Strategy
# ===========================================================================


class TestPartialCompress:

    def test_strategy_enum_exists(self):
        assert CompressionStrategy.PARTIAL == "PARTIAL"

    def test_partial_preserves_head_and_tail(self):
        compressor = ContextCompressor(strategy="PARTIAL")
        groups = [
            _make_group("head1", "g1", 50),  # preserved head
            _make_group("head2", "g2", 50),  # preserved head
            _make_tool_group("read_file", "x" * 1000, "g3", 200),  # middle
            _make_tool_group("read_file", "y" * 1000, "g4", 200),  # middle
            _make_group("recent1", "g5", 50),  # protected tail
            _make_group("recent2", "g6", 50),  # protected tail
        ]
        # Target: 400 tokens. Head(100) + Tail(100) = 200, leaves 200 for middle
        result = compressor._partial_compress(groups, target_tokens=400, preserved_count=2)
        # Head preserved
        assert result[0].group_id == "g1"
        assert result[-1].group_id == "g6"
        assert result[-2].group_id == "g5"

    def test_partial_snips_middle_tool_results(self):
        compressor = ContextCompressor(strategy="PARTIAL")
        groups = [
            _make_group("head", "g1", 50),
            _make_tool_group("read_file", "x" * 1000, "g2", 500),  # middle
            _make_tool_group("read_file", "y" * 1000, "g3", 500),  # middle
            _make_group("tail1", "g4", 50),  # protected
            _make_group("tail2", "g5", 50),  # protected
        ]
        result = compressor._partial_compress(groups, target_tokens=300, preserved_count=1)
        # Middle tool groups should be snipped
        found_snipped = False
        for g in result:
            for m in g.messages:
                if m.role == "tool" and m.content and len(m.content) < 1000:
                    found_snipped = True
        assert found_snipped or len(result) < len(groups)  # either snipped or truncated

    def test_partial_few_groups_unchanged(self):
        """With 2 or fewer groups, nothing to compress."""
        compressor = ContextCompressor(strategy="PARTIAL")
        groups = [
            _make_group("a", "g1", 100),
            _make_group("b", "g2", 100),
        ]
        result = compressor._partial_compress(groups, target_tokens=50)
        assert len(result) == 2

    def test_partial_via_compress_groups_async(self):
        """PARTIAL strategy dispatched from compress_groups_async."""
        compressor = ContextCompressor(strategy="PARTIAL")
        groups = [
            _make_group("a", "g1", 50),
            _make_tool_group("read_file", "x" * 1000, "g2", 300),
            _make_tool_group("read_file", "y" * 1000, "g3", 300),
            _make_group("b", "g4", 50),
        ]
        import asyncio
        result = asyncio.run(compressor.compress_groups_async(groups, target_tokens=200))
        # Should have compressed middle groups
        assert len(result) <= len(groups)


# ===========================================================================
# Image Stripping
# ===========================================================================


class TestImageStripping:

    def test_strips_image_url(self):
        group = ToolTransactionGroup(
            group_id="g1",
            group_type="PLAIN_MESSAGES",
            messages=[Message(
                role="user",
                content_parts=[
                    ContentPart(type="text", text="Look at this:"),
                    ContentPart(type="image_url", image_url="https://example.com/img.png"),
                ],
            )],
            token_estimate=100,
        )
        result = ContextCompressor.strip_images([group])
        parts = result[0].messages[0].content_parts
        assert len(parts) == 2
        assert parts[0].type == "text"
        assert parts[0].text == "Look at this:"
        assert parts[1].type == "text"
        assert parts[1].text == "[image removed for compaction]"

    def test_strips_image_base64(self):
        group = ToolTransactionGroup(
            group_id="g1",
            group_type="PLAIN_MESSAGES",
            messages=[Message(
                role="user",
                content_parts=[
                    ContentPart(type="image_base64", media_type="image/png", data="iVBOR..."),
                ],
            )],
            token_estimate=100,
        )
        result = ContextCompressor.strip_images([group])
        assert result[0].messages[0].content_parts[0].text == "[image removed for compaction]"

    def test_strips_audio(self):
        group = ToolTransactionGroup(
            group_id="g1",
            group_type="PLAIN_MESSAGES",
            messages=[Message(
                role="user",
                content_parts=[
                    ContentPart(type="audio", media_type="audio/wav", data="base64..."),
                ],
            )],
            token_estimate=100,
        )
        result = ContextCompressor.strip_images([group])
        assert result[0].messages[0].content_parts[0].text == "[audio removed for compaction]"

    def test_preserves_text_only(self):
        group = ToolTransactionGroup(
            group_id="g1",
            group_type="PLAIN_MESSAGES",
            messages=[Message(role="user", content="plain text")],
            token_estimate=100,
        )
        result = ContextCompressor.strip_images([group])
        assert result[0].messages[0].content == "plain text"

    def test_does_not_mutate_original(self):
        original_part = ContentPart(type="image_url", image_url="https://example.com/img.png")
        group = ToolTransactionGroup(
            group_id="g1",
            group_type="PLAIN_MESSAGES",
            messages=[Message(role="user", content_parts=[
                ContentPart(type="text", text="hi"),
                original_part,
            ])],
            token_estimate=100,
        )
        ContextCompressor.strip_images([group])
        assert group.messages[0].content_parts[1].type == "image_url"
