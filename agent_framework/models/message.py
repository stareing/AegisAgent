from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCallRequest(BaseModel):
    """A request to call a tool."""

    id: str
    function_name: str
    arguments: dict = Field(default_factory=dict)


class TokenUsage(BaseModel):
    """Token usage statistics."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


# ---------------------------------------------------------------------------
# Multimodal content parts
# ---------------------------------------------------------------------------

class ContentPart(BaseModel):
    """A single part of a multimodal message.

    Supported types:
    - text: plain text content
    - image_url: image referenced by URL (OpenAI vision format)
    - image_base64: inline base64-encoded image (Anthropic/Google format)
    - audio: base64-encoded audio data (OpenAI audio format)
    - file: generic file reference by URI

    Adapters convert these to provider-specific formats:
    - OpenAI: {"type": "text", ...} / {"type": "image_url", ...}
    - Anthropic: {"type": "text", ...} / {"type": "image", "source": {...}}
    - Google: {"text": ...} / {"inline_data": {"mime_type": ..., "data": ...}}

    Usage:
        # Text-only (equivalent to content="hello")
        Message(role="user", content_parts=[ContentPart(type="text", text="hello")])

        # Image from URL
        Message(role="user", content_parts=[
            ContentPart(type="text", text="What's in this image?"),
            ContentPart(type="image_url", image_url="https://example.com/photo.jpg"),
        ])

        # Inline base64 image
        Message(role="user", content_parts=[
            ContentPart(type="text", text="Describe this"),
            ContentPart(type="image_base64", media_type="image/png", data="iVBOR..."),
        ])
    """

    type: Literal["text", "image_url", "image_base64", "audio", "file"] = "text"

    # text
    text: str | None = None

    # image_url
    image_url: str | None = None
    detail: Literal["auto", "low", "high"] | None = None  # OpenAI vision detail

    # image_base64 / audio / file
    media_type: str | None = None  # e.g. "image/png", "audio/wav"
    data: str | None = None  # base64-encoded content

    # file (URI reference)
    file_uri: str | None = None


class Message(BaseModel):
    """A single message in conversation history.

    Multimodal support:
    - For text-only messages, use ``content: str`` (the common case).
    - For multimodal messages (images, audio), use ``content_parts: list[ContentPart]``.
    - If both are set, ``content_parts`` takes precedence in adapter conversion.
    - Adapters transparently convert content_parts to provider-specific formats.

    metadata boundary:
    - ``metadata`` is for INTERNAL framework use only (trace_id, timing, etc.).
    - metadata is NEVER sent to the LLM. ContextSourceProvider and ContextBuilder
      strip metadata when constructing messages for the model.
    - metadata is NEVER exposed to external APIs without explicit sanitization.
    - Only ``role``, ``content``, ``content_parts``, ``tool_calls``, ``tool_call_id``,
      and ``name`` are LLM-safe fields that may enter the model context.
    """

    # None semantics (project-wide convention):
    # - content: None = message has no text body (e.g. pure tool_calls assistant msg)
    # - content_parts: None = text-only message (use content field)
    # - tool_calls: None = this message does not invoke tools
    # - tool_call_id: None = not a tool response message
    # - name: None = no tool name (non-tool messages)
    # - metadata: None = no framework metadata attached
    # Rule: None means "does not exist", NOT "failed" or "empty string".
    #       Empty collections use [] not None; empty text uses "" not None.
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    content_parts: list[ContentPart] | None = None
    tool_calls: list[ToolCallRequest] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    metadata: dict | None = None
    # v4.3: Provider cache hint — tells supported providers (Anthropic) to cache
    # content up to this point. Not sent by providers that don't support it.
    # Example: {"type": "ephemeral"} for Anthropic prompt caching.
    cache_control: dict | None = None

    @property
    def has_multimodal(self) -> bool:
        """True if this message contains non-text content parts."""
        if not self.content_parts:
            return False
        return any(p.type != "text" for p in self.content_parts)

    @property
    def text_content(self) -> str | None:
        """Extract text content regardless of format.

        If content_parts is set, concatenates all text parts.
        Otherwise returns content.
        """
        if self.content_parts:
            texts = [p.text for p in self.content_parts if p.type == "text" and p.text]
            return "\n".join(texts) if texts else self.content
        return self.content


class ModelResponse(BaseModel):
    """Response from an LLM call."""

    content: str | None = None
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"
    usage: TokenUsage = Field(default_factory=TokenUsage)
    raw_response_meta: dict | None = None
