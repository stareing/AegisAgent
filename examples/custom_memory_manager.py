"""Example: Custom memory manager implementation.

Demonstrates how to create a completely custom memory manager by inheriting
BaseMemoryManager (replacement method B, section 11.9).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.memory.base_manager import BaseMemoryManager
from agent_framework.models.memory import (MemoryCandidate, MemoryKind,
                                           MemoryRecord, MemoryUpdateAction)

if TYPE_CHECKING:
    from agent_framework.models.agent import AgentState, IterationResult
    from agent_framework.protocols.core import MemoryStoreProtocol


class LLMMemoryManager(BaseMemoryManager):
    """A memory manager that uses LLM to decide what to remember.

    Instead of rule-based pattern matching, this manager sends the conversation
    to an LLM to extract memory-worthy information.

    This is a skeleton — fill in the LLM call with your preferred approach.
    """

    def __init__(
        self,
        store: MemoryStoreProtocol,
        max_memories_in_context: int = 10,
    ) -> None:
        super().__init__(store)
        self._max_in_context = max_memories_in_context

    def select_for_context(
        self, task: str, agent_state: AgentState
    ) -> list[MemoryRecord]:
        """Select memories using simple recency + pinned priority."""
        if not self._enabled or not self._agent_id:
            return []

        all_active = self._store.list_by_user(
            self._agent_id, self._user_id, active_only=True
        )
        pinned = [m for m in all_active if m.is_pinned]
        recent = self._store.list_recent(
            self._agent_id, self._user_id,
            limit=self._max_in_context - len(pinned),
        )
        recent = [m for m in recent if not m.is_pinned]

        result = pinned + recent
        return result[: self._max_in_context]

    def record_turn(
        self,
        user_input: str,
        final_answer: str | None,
        iteration_results: list[IterationResult],
    ) -> None:
        candidates = self.extract_candidates(user_input, final_answer, iteration_results)
        for c in candidates:
            self.remember(c)

    def extract_candidates(
        self,
        user_input: str,
        final_answer: str | None,
        iteration_results: list[IterationResult],
    ) -> list[MemoryCandidate]:
        """Placeholder: In a real implementation, call an LLM here.

        Example prompt to an LLM:
        "Given this conversation, extract any user preferences, constraints,
         or project context worth remembering long-term. Return as JSON."
        """
        # Skeleton — replace with actual LLM call
        return []

    def merge_candidate(
        self,
        candidate: MemoryCandidate,
        existing_records: list[MemoryRecord],
    ) -> MemoryUpdateAction:
        """Simple merge: always upsert unless exact duplicate."""
        for r in existing_records:
            if r.content.strip() == candidate.content.strip():
                return MemoryUpdateAction.IGNORE
        return MemoryUpdateAction.UPSERT


# Usage:
# from agent_framework.memory.sqlite_store import SQLiteMemoryStore
# store = SQLiteMemoryStore()
# manager = LLMMemoryManager(store=store)
