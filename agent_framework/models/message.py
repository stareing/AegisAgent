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
    """A single message in conversation history."""

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
