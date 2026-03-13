from __future__ import annotations

from pydantic import BaseModel, Field

from agent_framework.models.message import Message


class ContextStats(BaseModel):
    """Statistics about context construction."""

    system_tokens: int = 0
    memory_tokens: int = 0
    session_tokens: int = 0
    input_tokens: int = 0
    total_tokens: int = 0
    groups_trimmed: int = 0


class LLMRequest(BaseModel):
    """Encapsulates a prepared LLM request."""

    messages: list[Message] = Field(default_factory=list)
    tools_schema: list[dict] = Field(default_factory=list)
