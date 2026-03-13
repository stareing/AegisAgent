"""Memory policies for controlling memory behavior.

These are used by BaseAgent.get_memory_policy() and can be customized
per-agent or per-run.
"""

from __future__ import annotations

from pydantic import BaseModel


class MemoryPolicy(BaseModel):
    """Policy controlling memory behavior for an agent run."""

    enabled: bool = True
    auto_extract: bool = True
    max_in_context: int = 10
    allow_user_namespace: bool = True
    allow_management_api: bool = True


class MemoryQuota(BaseModel):
    """Quota limits for memory storage."""

    max_items_per_user: int = 200
    max_content_length: int = 2000
    max_tags_per_item: int = 10
