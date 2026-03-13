from __future__ import annotations

from typing import Literal

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


class Message(BaseModel):
    """A single message in conversation history.

    metadata boundary:
    - ``metadata`` is for INTERNAL framework use only (trace_id, timing, etc.).
    - metadata is NEVER sent to the LLM. ContextSourceProvider and ContextBuilder
      strip metadata when constructing messages for the model.
    - metadata is NEVER exposed to external APIs without explicit sanitization.
    - Only ``role``, ``content``, ``tool_calls``, ``tool_call_id``, and ``name``
      are LLM-safe fields that may enter the model context.
    """

    # None semantics (project-wide convention):
    # - content: None = message has no text body (e.g. pure tool_calls assistant msg)
    # - tool_calls: None = this message does not invoke tools
    # - tool_call_id: None = not a tool response message
    # - name: None = no tool name (non-tool messages)
    # - metadata: None = no framework metadata attached
    # Rule: None means "does not exist", NOT "failed" or "empty string".
    #       Empty collections use [] not None; empty text uses "" not None.
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCallRequest] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    metadata: dict | None = None


class ModelResponse(BaseModel):
    """Response from an LLM call."""

    content: str | None = None
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"
    usage: TokenUsage = Field(default_factory=TokenUsage)
    raw_response_meta: dict | None = None
