from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from agent_framework.models.message import Message


class ContextStats(BaseModel):
    """Statistics about context construction."""

    system_tokens: int = 0
    memory_tokens: int = 0
    session_tokens: int = 0
    input_tokens: int = 0
    tools_schema_tokens: int = 0
    total_tokens: int = 0
    groups_trimmed: int = 0
    prefix_reused: bool = False
    compression_strategy: str = ""


class FrozenPromptPrefix(BaseModel):
    """Immutable system prompt prefix for provider-side KV cache reuse (§14.8).

    Once created, `messages` MUST NOT be modified. The same system_core +
    skill_addon input must produce the same prefix_hash.

    Rules:
    - messages[0].role must be "system"
    - Frozen after construction — no field mutation
    - Compression/trimming may only operate on content AFTER the prefix
    - Prefix is rotated only on identity/skill/version change, NOT on
      memory/session/input changes
    """

    model_config = {"frozen": True}

    prefix_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    prefix_epoch: int = 0
    messages: list[Message] = Field(default_factory=list)
    prefix_hash: str = ""
    token_estimate: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_version: str = ""
    includes_skill_addon: bool = False


class LLMRequest(BaseModel):
    """Encapsulates a prepared LLM request."""

    messages: list[Message] = Field(default_factory=list)
    tools_schema: list[dict] = Field(default_factory=list)
    tools_schema_tokens: int = 0
